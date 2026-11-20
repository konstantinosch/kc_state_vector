"""Grouped pagination against hand-worked results, oracle and frozen item pager.

Tests keep row counts distinct from represented-member cardinality. Replaying a
JSON continuation on a fresh pager must be deterministic and must not mutate it.
"""

import copy
from dataclasses import replace
import json
import unittest

from reference.model import (
    Item, Pager as FrozenPager, Plan as FrozenPlan, Reservation, Stage as FrozenStage,
)
from reference.grouped.coverage import expand_coverage
from reference.grouped.model import Pager, Plan, Record, Stage
from reference.grouped.oracle import unpaged_oracle
from reference.grouped.scenarios import targeted_scenarios
from reference.grouped.tests.test_scenarios import EXPECTED_ROWS


SCHEDULES = (
    ((1,), (1,), (1,)),
    ((2,), (7,), (2,)),
    ((50,), (50,), (1000,)),
    ((1, 4, 2), (3, 1, 5), (1, 7, 2)),
)


class PagerTests(unittest.TestCase):
    def traverse(self, plan, representation, schedule=SCHEDULES[0]):
        sizes, batches, budgets = schedule
        state, rows, pages = None, [], []
        bound = sum(len(stage.candidate_ids) for stage in plan.stages) + len(plan.stages) + 10
        for step in range(bound):
            arguments = dict(page_size=sizes[step % len(sizes)],
                             batch_size=batches[step % len(batches)],
                             candidate_budget=budgets[step % len(budgets)])
            before = copy.deepcopy(state)
            page = Pager(plan, representation=representation).page(state, **arguments)
            self.assertEqual(state, before, "page() mutated the supplied continuation")
            retry_state = json.loads(json.dumps(before))
            replay = Pager(plan, representation=representation).page(retry_state, **arguments)
            self.assertEqual(retry_state, before, "retry mutated the supplied continuation")
            for attribute in ("rows", "state", "consumed", "fetched", "reads"):
                self.assertEqual(getattr(replay, attribute), getattr(page, attribute), attribute)
            self.assertLessEqual(len(page.rows), arguments["page_size"])
            self.assertLessEqual(page.consumed, arguments["candidate_budget"])
            self.assertGreaterEqual(page.consumed, len(page.rows))
            self.assertGreaterEqual(page.fetched, 0)
            self.assertGreaterEqual(page.reads, 0)
            for stage, representative, members in page.rows:
                self.assertIn(representative, members)
                self.assertTrue(members)
                self.assertIn(stage, {source.name for source in plan.stages})
            rows.extend(page.rows)
            pages.append(page)
            if page.state is None:
                represented = [identity for _, _, members in rows for identity in members]
                self.assertEqual(len(represented), len(set(represented)), "represented a member twice")
                return rows, pages
            self.assertNotEqual(page.state, before, "nonterminal continuation made no progress")
            state = json.loads(json.dumps(page.state))
        self.fail("fixed-data grouped traversal did not terminate within the source bound")

    def case(self, name):
        return next(case for case in targeted_scenarios() if case.name == name)

    def test_paired_representations_match_hand_worked_rows_and_oracle(self):
        for case in targeted_scenarios():
            oracle = unpaged_oracle(case.plan)
            self.assertEqual(oracle, EXPECTED_ROWS[case.name])
            for schedule in SCHEDULES:
                paired = []
                for representation in ("ids", "spans"):
                    with self.subTest(case=case.name, mode=representation, schedule=schedule):
                        rows, _ = self.traverse(case.plan, representation, schedule)
                        self.assertEqual(rows, oracle)
                        paired.append(rows)
                self.assertEqual(paired[0], paired[1])

    def test_cursor_crossing_does_not_retire_coverage_needed_by_group_expansion(self):
        plan = self.case("resolver_reachability").plan
        for representation in ("ids", "spans"):
            with self.subTest(mode=representation):
                pager = Pager(plan, representation=representation)
                first = pager.page(page_size=1, candidate_budget=1)
                self.assertEqual(first.rows, (("first", "A", ("A",)),))
                second = pager.page(first.state, page_size=1, candidate_budget=1)
                self.assertEqual(second.rows, (("grouped", "B", ("B",)),))
                self.assertIsNotNone(second.state)
                self.assertEqual(second.state["cursors"][1], [90, "B"])
                retained = expand_coverage(plan.records, second.state["coverage"], representation)
                self.assertIn("A", retained, "C below the cursor can still expand to already selected A")
                third = Pager(plan, representation=representation).page(
                    json.loads(json.dumps(second.state)), page_size=1, candidate_budget=1)
                self.assertEqual(third.rows, (("grouped", "C", ("C",)),))

    def test_consumed_deferred_owner_keeps_pending_claims_until_completion(self):
        for completion in ("release", "omit"):
            plan = self.case(f"overlapping_defer_{completion}").plan
            for representation in ("ids", "spans"):
                with self.subTest(mode=representation, completion=completion):
                    pager = Pager(plan, representation=representation)
                    first = pager.page(page_size=50, candidate_budget=1)
                    self.assertEqual(first.rows, ())
                    second = pager.page(first.state, page_size=50, candidate_budget=1)
                    self.assertEqual(second.rows, ())
                    self.assertIsNotNone(second.state)
                    self.assertEqual(second.state["cursors"][1], [90, "B"])
                    self.assertTrue({"B", "C", "D"}.issubset(set(second.state["pending"])))
                    rows, _ = self.traverse(plan, representation)
                    self.assertEqual(rows, EXPECTED_ROWS[f"overlapping_defer_{completion}"])

    def test_reservation_holes_survive_group_coverage(self):
        plan = self.case("middle_hole_subtract").plan
        for representation in ("ids", "spans"):
            with self.subTest(mode=representation):
                first = Pager(plan, representation=representation).page(page_size=1, candidate_budget=1)
                self.assertEqual(first.rows, (("early", "A", ("A", "C")),))
                retained = expand_coverage(plan.records, first.state["coverage"], representation)
                self.assertEqual(retained, {"A", "C"})
                self.assertNotIn("B", retained)
                self.assertIn("B", first.state["pending"])

    def test_group_membership_expands_beyond_candidate_membership(self):
        plan = self.case("candidate_scope_and_final_grouping").plan
        self.assertEqual(plan.stages[0].candidate_ids, ("A",))
        for representation in ("ids", "spans"):
            with self.subTest(mode=representation):
                first = Pager(plan, representation=representation).page(page_size=1, candidate_budget=1)
                self.assertEqual(first.rows, (("priority", "A", ("A", "B", "C")),))
                self.assertEqual(first.consumed, 1)
                rows, _ = self.traverse(plan, representation)
                self.assertEqual(rows[-1], ("last", "X", ("X", "Y")))

    def test_owner_can_reserve_a_member_reached_only_through_group_expansion(self):
        records = (Record("A", 100, "G"), Record("B", 90, "G"), Record("C", 80, "H"))
        plan = Plan(records,
                    (Stage("early", ("B", "C"), mode="items"), Stage("owner", ("A",)),
                     Stage("rest", ("A", "B", "C"), mode="items")),
                    (Reservation("owner", ("B",)),))
        expected = [("early", "C", ("C",)), ("owner", "A", ("A", "B"))]
        self.assertEqual(unpaged_oracle(plan), expected)
        self.assertNotIn("B", plan.stages[1].candidate_ids)
        for representation in ("ids", "spans"):
            for schedule in SCHEDULES:
                with self.subTest(mode=representation, schedule=schedule):
                    rows, _ = self.traverse(plan, representation, schedule)
                    self.assertEqual(rows, expected)

    def test_item_only_extension_matches_the_unchanged_frozen_pager(self):
        for case in targeted_scenarios():
            plan = Plan(case.plan.records,
                        tuple(replace(stage, mode="items") for stage in case.plan.stages),
                        case.plan.reservations, scope=case.plan.scope)
            lookup = {record.id: Item(record.id, record.sort_key) for record in plan.records}
            frozen = FrozenPlan(
                tuple(FrozenStage(stage.name, tuple(lookup[identity] for identity in stage.candidate_ids),
                                  quota=stage.quota) for stage in plan.stages),
                plan.reservations, scope=plan.scope)
            for page_size, budget in ((1, 1), (50, 1000)):
                state, expected = None, []
                bound = sum(len(stage.items) for stage in frozen.stages) + len(frozen.stages) + 10
                for _ in range(bound):
                    page = FrozenPager(frozen).page(state, page_size=page_size,
                                                   batch_size=7, candidate_budget=budget)
                    expected.extend((stage, identity, (identity,)) for stage, identity in page.rows)
                    if page.state is None:
                        break
                    state = json.loads(json.dumps(page.state))
                else:
                    self.fail("frozen reference did not terminate")
                for representation in ("ids", "spans"):
                    with self.subTest(case=case.name, mode=representation,
                                      page_size=page_size, budget=budget):
                        actual, _ = self.traverse(plan, representation,
                                                  ((page_size,), (7,), (budget,)))
                        self.assertEqual(actual, expected)
                        self.assertEqual(unpaged_oracle(plan), expected)


if __name__ == "__main__":
    unittest.main()
