"""Hybrid is an admission-time byte choice, with exact fixed-descriptor coverage."""

import copy
import json
import unittest

from reference.model import CapacityError, StateError
from reference.grouped.coverage import expand_coverage
from reference.grouped.hybrid_scenarios import hybrid_scenarios
from reference.grouped.model import Pager
from reference.grouped.oracle import unpaged_oracle
from reference.grouped.scenarios import targeted_scenarios


def json_bytes(value):
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8"))


class HybridPagerTests(unittest.TestCase):
    def case(self, name):
        return next(case for case in hybrid_scenarios() if case.name == name)

    def traverse(self, plan, mode):
        state, output = None, []
        bound = sum(len(stage.candidate_ids) for stage in plan.stages) + len(plan.stages) + 10
        for step in range(bound):
            arguments = dict(page_size=(1, 50)[step % 2], batch_size=(4, 1)[step % 2],
                             candidate_budget=(1, 7)[step % 2])
            incoming = copy.deepcopy(state)
            page = Pager(plan, representation=mode).page(state, **arguments)
            retry_input = json.loads(json.dumps(incoming))
            replay = Pager(plan, representation=mode).page(retry_input, **arguments)
            self.assertEqual(page, replay)
            self.assertEqual(state, incoming)
            self.assertEqual(retry_input, incoming)
            self.assertLessEqual(len(page.rows), arguments["page_size"])
            self.assertLessEqual(page.consumed, arguments["candidate_budget"])
            output.extend(page.rows)
            if page.state is None:
                represented = [identity for _, _, members in output for identity in members]
                self.assertEqual(len(represented), len(set(represented)))
                return output
            self.assertNotEqual(page.state, incoming)
            state = json.loads(json.dumps(page.state))
        self.fail("Hybrid fixture did not terminate within the finite source bound")

    def test_all_hybrid_fixtures_match_both_baselines_and_oracle(self):
        cases = hybrid_scenarios()
        self.assertEqual(len(cases), 6)
        self.assertEqual(len({case.name for case in cases}), 6)
        for case in cases:
            expected = unpaged_oracle(case.plan)
            paired = []
            for mode in ("ids", "spans", "hybrid"):
                with self.subTest(case=case.name, mode=mode):
                    result = self.traverse(case.plan, mode)
                    self.assertEqual(result, expected)
                    paired.append(result)
            self.assertEqual(paired[0], paired[1])
            self.assertEqual(paired[1], paired[2])

    def test_hybrid_preserves_existing_retirement_and_deferred_owner_regressions(self):
        names = {"resolver_reachability", "overlapping_defer_release", "overlapping_defer_omit",
                 "episode_holes", "selective_yield_preemption"}
        for case in targeted_scenarios():
            if case.name in names:
                with self.subTest(case=case.name):
                    self.assertEqual(self.traverse(case.plan, "hybrid"), unpaged_oracle(case.plan))

    def test_one_row_mixes_span_and_bare_id_without_covering_reserved_hole(self):
        plan = self.case("hybrid_mixture_subtract").plan
        first = Pager(plan, representation="hybrid").page(page_size=1, candidate_budget=1)
        self.assertEqual(len(first.rows), 1)
        self.assertEqual(len(first.rows[0][2]), 21)
        entries = first.state["coverage"]
        spans = [entry for entry in entries if type(entry) is list]
        ids = [entry for entry in entries if type(entry) is str]
        self.assertEqual(len(spans), 1)
        self.assertEqual(len(ids), 1)
        self.assertEqual(ids, [plan.records[-1].id])
        self.assertEqual(spans[0][0], "span")
        expected = {record.id for record in plan.records} - {"reserved-middle"}
        self.assertEqual(expand_coverage(plan.records, entries, "hybrid"), expected)
        self.assertEqual(first.state["pending"], ["reserved-middle"])
        explicit = Pager(plan, representation="ids").page(page_size=1, candidate_budget=1)
        self.assertLess(json_bytes(entries), json_bytes(explicit.state["coverage"]))

    def test_partial_reservation_policies_change_rows_but_not_selected_members(self):
        for prefix, count in (("hybrid_mixture", 22), ("hybrid_unicode_ties", 20)):
            for policy in ("subtract", "defer"):
                with self.subTest(prefix=prefix, policy=policy):
                    plan = self.case(f"{prefix}_{policy}").plan
                    rows = self.traverse(plan, "hybrid")
                    self.assertEqual([row[0] for row in rows],
                                     ["early", "owner"] if policy == "subtract" else ["owner"])
                    represented = [identity for _, _, members in rows for identity in members]
                    self.assertEqual(len(represented), count)
                    self.assertEqual(set(represented), {record.id for record in plan.records})

    def test_unicode_tuple_ties_keep_escaped_cost_and_reservation_hole_exact(self):
        plan = self.case("hybrid_unicode_ties_subtract").plan
        first = Pager(plan, representation="hybrid").page(page_size=1, candidate_budget=1)
        reserved = plan.records[10].id
        entries = first.state["coverage"]
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(type(entry) is list and entry[0] == "span" for entry in entries))
        self.assertEqual(first.rows[0][1], max(record.id for record in plan.records))
        self.assertEqual(first.rows[0][2],
                         tuple(sorted((record.id for record in plan.records if record.id != reserved), reverse=True)))
        self.assertNotIn(reserved, expand_coverage(plan.records, entries, "hybrid"))
        for entry in entries:
            self.assertEqual(entry[2][0], 42)
            self.assertEqual(entry[3][0], 42)
            self.assertLess(entry[2][1], entry[3][1])
            represented = expand_coverage(plan.records, [entry], "hybrid")
            self.assertLess(json_bytes([entry]), json_bytes(sorted(represented)))
        self.assertIn("\\ud83d", json.dumps(entries, ensure_ascii=True))

    def test_long_group_name_makes_ids_cheaper_but_can_exhaust_an_entry_cap(self):
        plan = self.case("hybrid_short_ids_long_group").plan
        args = dict(page_size=1, candidate_budget=1)
        hybrid = Pager(plan, representation="hybrid").page(**args)
        spans = Pager(plan, representation="spans", max_state_entries=1).page(**args)
        self.assertEqual(set(hybrid.state["coverage"]), {"a", "b", "c"})
        self.assertEqual(len(spans.state["coverage"]), 1)
        self.assertLess(json_bytes(hybrid.state["coverage"]), json_bytes(spans.state["coverage"]))
        self.assertEqual(hybrid.rows, spans.rows)
        with self.assertRaises(CapacityError):
            Pager(plan, representation="hybrid", max_state_entries=1).page(**args)
        retry = Pager(plan, representation="hybrid", max_state_entries=3).page(**args)
        self.assertEqual(retry, hybrid)

    def test_fixed_span_can_lose_its_admission_advantage_later_without_switching(self):
        plan = self.case("hybrid_fixed_span_lifetime").plan
        initial, late = {}, {}
        for mode in ("ids", "hybrid"):
            initial[mode] = Pager(plan, representation=mode).page(page_size=1, candidate_budget=1)
            late[mode] = Pager(plan, representation=mode).page(
                json.loads(json.dumps(initial[mode].state)), page_size=50, candidate_budget=19)
            self.assertEqual(late[mode].rows, ())
            self.assertEqual(late[mode].consumed, 19)
        admitted = initial["hybrid"].state["coverage"]
        self.assertEqual(len(admitted), 1)
        self.assertEqual(admitted[0][0], "span")
        self.assertLess(json_bytes(admitted), json_bytes(initial["ids"].state["coverage"]))
        self.assertEqual(late["hybrid"].state["coverage"], admitted)
        self.assertEqual(late["ids"].state["coverage"], [plan.records[19].id])
        self.assertGreater(json_bytes(admitted), json_bytes(late["ids"].state["coverage"]))
        self.assertIn(plan.records[19].id,
                      expand_coverage(plan.records, late["hybrid"].state["coverage"], "hybrid"))
        retired = Pager(plan, representation="hybrid").page(late["hybrid"].state,
                                                             page_size=50, candidate_budget=1)
        self.assertEqual(retired.rows, ())
        self.assertIsNotNone(retired.state)
        self.assertEqual(retired.state["coverage"], [])
        final = Pager(plan, representation="hybrid").page(retired.state, page_size=50, candidate_budget=1)
        self.assertEqual(final.rows, (("sweep", "tail", ("tail",)),))
        self.assertIsNone(final.state)

    def test_continuation_is_bound_to_its_representation(self):
        plan = self.case("hybrid_mixture_subtract").plan
        for produced in ("ids", "spans", "hybrid"):
            state = Pager(plan, representation=produced).page(page_size=1, candidate_budget=1).state
            before = copy.deepcopy(state)
            for consumed in ("ids", "spans", "hybrid"):
                if consumed != produced:
                    with self.subTest(produced=produced, consumed=consumed):
                        with self.assertRaises(StateError):
                            Pager(plan, representation=consumed).page(state)
                        self.assertEqual(state, before)

    def test_exact_emitted_capacity_boundary_and_one_below_preserve_retry_input(self):
        plan = self.case("hybrid_mixture_subtract").plan
        incoming = Pager(plan, representation="hybrid").page(page_size=1, candidate_budget=1).state
        before = copy.deepcopy(incoming)
        args = dict(page_size=50, candidate_budget=1)
        expected = Pager(plan, representation="hybrid").page(incoming, **args)
        self.assertIsNotNone(expected.state)
        entries = sum(len(expected.state[key]) for key in ("coverage", "pending", "omitted"))
        byte_count = json_bytes(expected.state)
        self.assertGreater(entries, 1)
        exact = Pager(plan, representation="hybrid", max_state_entries=entries,
                      max_state_bytes=byte_count).page(incoming, **args)
        self.assertEqual(exact, expected)
        for limits in (dict(max_state_entries=entries - 1), dict(max_state_bytes=byte_count - 1)):
            with self.subTest(limits=limits):
                with self.assertRaises(CapacityError):
                    Pager(plan, representation="hybrid", **limits).page(incoming, **args)
                self.assertEqual(incoming, before)
                retry = Pager(plan, representation="hybrid").page(
                    json.loads(json.dumps(incoming)), **args)
                self.assertEqual(retry, expected)


if __name__ == "__main__":
    unittest.main()
