"""Deterministic, countable checks for the local-only reference model.

Run from the repository root with:
    python -m unittest reference.tests.test_reference -v

The generated matrix is 40 data sets x 27 schedules = 1,080 traversals.
Thirty additional baseline cases reuse the vendored legacy_helpers.unpaged_staged_oracle;
thirty prepared-source cases reuse the vendored legacy_helpers.prepared_buffer_model.
"""

from __future__ import annotations

import copy
from dataclasses import replace
import json
import random
import unittest

from reference.model import (
    Batch, CapacityError, Item, MemorySource, Pager, Plan, PlanError,
    ReadError, Reservation, Stage, StateError,
)
from reference.oracle import unpaged_oracle
from reference.tests.legacy_helpers import prepared_buffer_model, unpaged_staged_oracle


def items(*ids: str) -> tuple[Item, ...]:
    """Alphabetical IDs have immutable, descending alphabet-based positions."""
    return tuple(sorted((Item(item_id, 100 - ord(item_id[0])) for item_id in ids),
                        key=lambda item: item.position, reverse=True))


def make_stage(name, *ids, quota=None, buffer_size=None):
    return Stage(name, items(*ids), quota=quota, buffer_size=buffer_size)


class RecordingSource(MemorySource):
    def __init__(self):
        self.calls = []

    def read(self, stage, cursor, limit):
        result = super().read(stage, cursor, limit)
        self.calls.append((stage.name, cursor, limit, result))
        return result


class ReferenceTests(unittest.TestCase):
    def traverse(self, plan, *, sizes=(7,), batches=(4,), budgets=(13,),
                 state=None, pager=None, roundtrip=True):
        pager = Pager(plan) if pager is None else pager
        result, pages = [], []
        # Every candidate is processed at most once in the immutable sources;
        # completion-only pages may add at most one page per stage.
        upper_bound = sum(len(stage.items) for stage in plan.stages) + len(plan.stages) + 10
        for step in range(upper_bound):
            size = sizes[step % len(sizes)]
            batch = batches[step % len(batches)]
            budget = budgets[step % len(budgets)]
            before = copy.deepcopy(state)
            page = pager.page(state, page_size=size, batch_size=batch, candidate_budget=budget)
            self.assertEqual(state, before, "page() mutated its supplied continuation")
            self.assertLessEqual(len(page.rows), size)
            self.assertLessEqual(page.consumed, budget)
            self.assertGreaterEqual(page.consumed, len(page.rows))
            self.assertGreaterEqual(page.fetched, 0)
            self.assertGreaterEqual(page.reads, 0)
            self.assertEqual(list(page.ids), [item_id for _, item_id in page.rows])
            result.extend(page.rows)
            pages.append(page)
            if page.state is None:
                self.assertEqual(len(result), len({item_id for _, item_id in result}))
                return result, pages
            self.assertNotEqual(page.state, before, "continuation made no progress")
            state = json.loads(json.dumps(page.state)) if roundtrip else page.state
        self.fail("fixed-data traversal did not terminate within its raw-candidate bound")

    def assert_sequence(self, plan, expected):
        self.assertEqual(unpaged_oracle(plan), expected)
        for sizes, batches, budgets in (
            ((1,), (1,), (1,)), ((2,), (7,), (2,)),
            ((50,), (50,), (1000,)), ((1, 4, 2), (3, 1, 5), (1, 7, 2)),
        ):
            with self.subTest(sizes=sizes, batches=batches, budgets=budgets):
                actual, _ = self.traverse(plan, sizes=sizes, batches=batches, budgets=budgets)
                self.assertEqual(actual, expected)

    def test_core_stage_exclusion_and_quotas(self):
        plan = Plan((make_stage("first", "A", "B", quota=1),
                     make_stage("second", "A", "C", quota=1),
                     make_stage("rest", "A", "B", "C", "D")))
        self.assert_sequence(plan, [("first", "A"), ("second", "C"), ("rest", "B"), ("rest", "D")])

    def test_reservations_change_attribution_without_selecting_early(self):
        plan = Plan((make_stage("first", "A", "B", "C", quota=2),
                     make_stage("owner", "A", "D", quota=2),
                     make_stage("rest", "A", "B", "C", "D", "E")),
                    (Reservation("owner", ("A",)),))
        first = Pager(plan).page(page_size=1, batch_size=3, candidate_budget=1)
        self.assertEqual(first.rows, ())
        self.assertEqual(first.consumed, 1)
        self.assertIsNotNone(first.state)
        self.assert_sequence(plan, [("first", "B"), ("first", "C"),
                                    ("owner", "A"), ("owner", "D"), ("rest", "E")])

    def test_reserved_late_position_is_not_promoted_ahead_of_source_order(self):
        plan = Plan((make_stage("first", "C"), make_stage("owner", "A", "B", "C")),
                    (Reservation("owner", ("C",)),))
        self.assert_sequence(plan, [("owner", "A"), ("owner", "B"), ("owner", "C")])

    def test_zero_owner_quota_omit_and_release(self):
        stages = (make_stage("before", "A", "B"), make_stage("owner", "A", quota=0),
                  make_stage("after", "A", "B", "C"))
        for policy, expected in (
            ("omit", [("before", "B"), ("after", "C")]),
            ("release", [("before", "B"), ("after", "A"), ("after", "C")]),
        ):
            with self.subTest(policy=policy):
                self.assert_sequence(Plan(stages, (Reservation("owner", ("A",), on_unserved=policy),)), expected)

    def test_insufficient_owner_quota_omit_and_release(self):
        stages = (make_stage("before", "A", "C", "D"),
                  make_stage("owner", "A", "B", "C", quota=1),
                  make_stage("after", "A", "B", "C", "D", "E"))
        for policy, suffix in (("omit", [("after", "B"), ("after", "E")]),
                               ("release", [("after", "B"), ("after", "C"), ("after", "E")])):
            with self.subTest(policy=policy):
                plan = Plan(stages, (Reservation("owner", ("A", "C"), on_unserved=policy),))
                self.assert_sequence(plan, [("before", "D"), ("owner", "A")] + suffix)

    def test_release_never_rewinds_an_earlier_source(self):
        for policy in ("omit", "release"):
            plan = Plan((make_stage("before", "A", "B"), make_stage("owner", "A", quota=0),
                         make_stage("after", "C")),
                        (Reservation("owner", ("A",), on_unserved=policy),))
            self.assert_sequence(plan, [("before", "B"), ("after", "C")])

    def test_unaffected_earlier_stage_covers_owner_without_spending_owner_quota(self):
        for policy in ("omit", "release"):
            plan = Plan((make_stage("unaffected", "A"), make_stage("yielding", "A", "B"),
                         make_stage("owner", "A", "C", quota=1), make_stage("rest", "A", "D")),
                        (Reservation("owner", ("A",), yield_from=("yielding", "rest"), on_unserved=policy),))
            self.assert_sequence(plan, [("unaffected", "A"), ("yielding", "B"),
                                        ("owner", "C"), ("rest", "D")])

    def test_already_covered_owner_with_zero_quota_keeps_duplicate_excluded(self):
        plan = Plan((make_stage("unaffected", "A"), make_stage("owner", "A", quota=0),
                     make_stage("rest", "A", "B")),
                    (Reservation("owner", ("A",), yield_from=("rest",), on_unserved="omit"),))
        self.assert_sequence(plan, [("unaffected", "A"), ("rest", "B")])

    def test_overlap_winner_uses_plan_order_and_winner_completion_policy(self):
        stages = (make_stage("before", "A", "D"), make_stage("early_owner", "A", quota=0),
                  make_stage("late_owner", "A", "B"), make_stage("rest", "A", "C"))
        for winner_policy in ("omit", "release"):
            reservations = (
                Reservation("late_owner", ("A",), on_unserved="release" if winner_policy == "omit" else "omit"),
                Reservation("early_owner", ("A",), on_unserved=winner_policy),
            )
            expected = [("before", "D")]
            if winner_policy == "release":
                expected.append(("late_owner", "A"))
            expected.extend([("late_owner", "B"), ("rest", "C")])
            self.assert_sequence(Plan(stages, reservations), expected)
            self.assertEqual(unpaged_oracle(Plan(stages, tuple(reversed(reservations)))), expected)

    def test_overlap_uses_winners_yield_set_and_does_not_revive_losing_claim(self):
        stages = (make_stage("before", "A", "B"), make_stage("winner", "A", quota=0),
                  make_stage("middle", "A"), make_stage("loser", "A"))
        claims = (Reservation("loser", ("A",)),
                  Reservation("winner", ("A",), yield_from=("before",), on_unserved="release"))
        self.assert_sequence(Plan(stages, claims), [("before", "B"), ("middle", "A")])
        preempt = replace(claims[1], yield_from=("middle",))
        self.assert_sequence(Plan(stages, (claims[0], preempt)), [("before", "A"), ("before", "B")])

    def test_full_tuple_ties_and_negative_sort_keys(self):
        tied = tuple(Item(item_id, 7) for item_id in ("z", "m", "a")) + (Item("negative", -1),)
        plan = Plan((Stage("first", tied, 2), Stage("rest", tied)))
        self.assert_sequence(plan, [("first", "z"), ("first", "m"), ("rest", "a"), ("rest", "negative")])

    def test_empty_sources_and_zero_quotas_terminate(self):
        for plan in (Plan(()), Plan((make_stage("empty"),)),
                     Plan((make_stage("zero", "A", quota=0), make_stage("empty")))):
            self.assert_sequence(plan, [])

    def test_empty_page_advances_and_has_a_successor(self):
        plan = Plan((make_stage("first", "A", "B"), make_stage("rest", "A", "B", "C")))
        pager = Pager(plan)
        first = pager.page(page_size=2, batch_size=8, candidate_budget=2)
        self.assertEqual(first.ids, ("A", "B"))
        middle = pager.page(first.state, page_size=2, batch_size=8, candidate_budget=1)
        self.assertEqual(middle.rows, ())
        self.assertEqual(middle.consumed, 1)
        self.assertIsNotNone(middle.state)
        self.assertNotEqual(first.state, middle.state)
        rest, _ = self.traverse(plan, state=middle.state, pager=pager, budgets=(1,))
        self.assertEqual(list(first.rows) + rest, unpaged_oracle(plan))

    def test_prepared_source_carries_fetched_tail_and_refills_after_skips(self):
        plan = Plan((make_stage("first", "A", quota=1),
                     make_stage("prepared", "A", "B", "C", "D", "E", buffer_size=3, quota=4)))
        source = RecordingSource()
        pager = Pager(plan, source=source)
        first = pager.page(page_size=1, batch_size=3, candidate_budget=10)
        second = pager.page(first.state, page_size=1, batch_size=3, candidate_budget=10)
        self.assertEqual(second.ids, ("B",))
        before_calls = len(source.calls)
        third = pager.page(second.state, page_size=1, batch_size=3, candidate_budget=10)
        self.assertEqual(third.ids, ("C",))
        self.assertEqual(len(source.calls), before_calls, "prepared unconsumed tail should already be retained")
        rest, _ = self.traverse(plan, state=third.state, pager=pager)
        self.assertEqual(list(first.rows + second.rows + third.rows) + rest, unpaged_oracle(plan))

    def test_prepared_fetch_cursor_cannot_prune_coverage_for_buffered_items(self):
        plan = Plan((make_stage("prior", "C"),
                     make_stage("prepared", "A", "B", "C", "D", buffer_size=4)))
        self.assert_sequence(plan, [("prior", "C"), ("prepared", "A"),
                                    ("prepared", "B"), ("prepared", "D")])

    def test_ordinary_source_refetches_unconsumed_tail(self):
        plan = Plan((make_stage("plain", "A", "B", "C", "D"),))
        source = RecordingSource()
        pager = Pager(plan, source=source)
        first = pager.page(page_size=1, batch_size=3, candidate_budget=10)
        second = pager.page(first.state, page_size=1, batch_size=3, candidate_budget=10)
        self.assertEqual(first.ids + second.ids, ("A", "B"))
        self.assertEqual(len(source.calls), 2)
        self.assertEqual(source.calls[1][1], items("A")[0].position)
        self.assertEqual(first.fetched, 3)
        self.assertEqual(first.consumed, 1)

    def test_candidate_budget_counts_skips_separately_from_fetches(self):
        plan = Plan((make_stage("before", "A", "B", "C", "D"),
                     make_stage("owner", "A", "B", "C", "D")),
                    (Reservation("owner", ("A", "B", "C")),))
        source = RecordingSource()
        first = Pager(plan, source=source).page(page_size=50, batch_size=4, candidate_budget=2)
        self.assertEqual(first.rows, ())
        self.assertEqual(first.consumed, 2)
        self.assertEqual(first.fetched, sum(len(call[3].items) for call in source.calls))
        self.assertEqual(first.reads, len(source.calls))

    def test_prepared_consumption_can_exceed_fetched_entries_on_a_later_page(self):
        plan = Plan((make_stage("prepared", "A", "B", "C", "D", buffer_size=4),))
        pager = Pager(plan)
        first = pager.page(page_size=1, batch_size=4, candidate_budget=10)
        self.assertEqual(first.fetched, 4)
        self.assertEqual(first.consumed, 1)
        second = pager.page(first.state, page_size=1, batch_size=4, candidate_budget=1)
        self.assertEqual(second.ids, ("B",))
        self.assertEqual(second.consumed, 1)
        self.assertEqual(second.fetched, 0)
        self.assertEqual(second.reads, 0)

    def test_json_roundtrip_replay_and_new_pager_are_identical(self):
        plan = Plan((make_stage("before", "A", "B", "C"),
                     make_stage("prepared", "A", "B", "C", "D", "E", buffer_size=3)),
                    (Reservation("prepared", ("B",)),))
        pager = Pager(plan)
        first = pager.page(page_size=1, candidate_budget=2)
        state = json.loads(json.dumps(first.state))
        saved = copy.deepcopy(state)
        kwargs = dict(page_size=2, batch_size=3, candidate_budget=5)
        second = pager.page(state, **kwargs)
        self.assertEqual(pager.page(state, **kwargs), second)
        self.assertEqual(Pager(plan).page(state, **kwargs), second)
        self.assertEqual(state, saved)

    def test_capacity_error_preserves_input_and_can_resume_with_more_capacity(self):
        plan = Plan((make_stage("first", "A", "B", "C", "D"),
                     make_stage("rest", "A", "B", "C", "D")))
        first = Pager(plan).page(page_size=1)
        saved = copy.deepcopy(first.state)
        constrained = Pager(plan, max_state_entries=1)
        for _ in range(2):
            with self.assertRaises(CapacityError):
                constrained.page(first.state, page_size=2)
            self.assertEqual(first.state, saved)
        rest, _ = self.traverse(plan, state=first.state)
        self.assertEqual(list(first.rows) + rest, unpaged_oracle(plan))

    def test_byte_capacity_fails_without_silent_eviction(self):
        plan = Plan((make_stage("first", "A", "B", "C"), make_stage("rest", "A", "B", "C")))
        state = Pager(plan).page(page_size=1).state
        saved = copy.deepcopy(state)
        with self.assertRaises(CapacityError):
            Pager(plan, max_state_bytes=1).page(state, page_size=1)
        self.assertEqual(state, saved)

    def test_incomplete_source_response_rolls_back_tentative_page(self):
        plan = Plan((make_stage("source", "A", "B", "C", "D"),))
        state = Pager(plan).page(page_size=1, batch_size=1).state
        saved = copy.deepcopy(state)

        class IncompleteSecondRead(MemorySource):
            def __init__(self):
                self.calls = 0

            def read(self, stage, cursor, limit):
                self.calls += 1
                batch = super().read(stage, cursor, limit)
                return replace(batch, complete=False) if self.calls == 2 else batch

        with self.assertRaises(ReadError):
            Pager(plan, source=IncompleteSecondRead()).page(state, page_size=2, batch_size=1)
        self.assertEqual(state, saved)
        rest, _ = self.traverse(plan, state=state)
        self.assertEqual(rest, [("source", "B"), ("source", "C"), ("source", "D")])

    def test_source_exception_rolls_back_tentative_page(self):
        plan = Plan((make_stage("source", "A", "B", "C"),))
        state = Pager(plan).page(page_size=1, batch_size=1).state
        saved = copy.deepcopy(state)

        class FailingSource(MemorySource):
            def read(self, stage, cursor, limit):
                raise OSError("synthetic read failure")

        with self.assertRaises(ReadError):
            Pager(plan, source=FailingSource()).page(state)
        self.assertEqual(state, saved)

    def test_malformed_source_batches_are_rejected(self):
        plan = Plan((make_stage("source", "A", "B", "C"),))
        a, b, c = plan.stages[0].items

        class FixedSource:
            def __init__(self, batch):
                self.batch = batch

            def read(self, stage, cursor, limit):
                return self.batch

        for batch, limit in ((Batch((a, a), True), 2), (Batch((b, a), True), 2),
                             (Batch((), False), 2), (Batch((a, b, c), True), 2),
                             (Batch((Item("outside", 999),), True), 2),
                             (Batch((a,), True, complete=False), 2)):
            with self.subTest(batch=batch, limit=limit):
                with self.assertRaises(ReadError):
                    Pager(plan, source=FixedSource(batch)).page(batch_size=limit)

    def test_state_binding_rejects_changed_plan_and_scope(self):
        plan = Plan((make_stage("first", "A", "B", quota=1), make_stage("rest", "A", "B", "C")))
        state = Pager(plan).page(page_size=1).state
        variants = (replace(plan, scope="other"),
                    replace(plan, stages=(replace(plan.stages[0], quota=2), plan.stages[1])),
                    replace(plan, stages=(plan.stages[0], make_stage("rest", "A", "B", "C", "D"))),
                    replace(plan, reservations=(Reservation("rest", ("B",)),)),
                    replace(plan, stages=(plan.stages[0], replace(plan.stages[1], buffer_size=2))))
        for variant in variants:
            with self.subTest(variant=variant):
                with self.assertRaises(StateError):
                    Pager(variant).page(state)

    def test_malformed_state_shapes_are_rejected(self):
        plan = Plan((make_stage("first", "A", "B"), make_stage("rest", "A", "B", "C")))
        state = Pager(plan).page(page_size=1).state
        self.assertIsInstance(state, dict)
        malformed = [{}, [], "not-state", True, {**state, "unexpected": 1}]
        # Removing a required field should not silently reset any continuation.
        malformed.extend({key: value for key, value in state.items() if key != missing} for missing in state)
        for candidate in malformed:
            with self.subTest(candidate=candidate):
                with self.assertRaises(StateError):
                    Pager(plan).page(candidate)

    def test_invalid_limits_are_rejected(self):
        pager = Pager(Plan((make_stage("source", "A"),)))
        for field in ("page_size", "batch_size", "candidate_budget"):
            for value in (0, -1, True, 1.5, "2"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        pager.page(**{field: value})

    def test_invalid_plans_are_rejected(self):
        bad_plan_factories = (
            lambda: Plan((make_stage("same", "A"), make_stage("same", "B"))),
            lambda: Plan((Stage("duplicate", (Item("A", 2), Item("A", 2))),)),
            lambda: Plan((Stage("one", (Item("A", 2),)), Stage("two", (Item("A", 1),)))),
            lambda: Plan((make_stage("one", "A", quota=-1),)),
            lambda: Plan((make_stage("one", "A", quota=True),)),
            lambda: Plan((make_stage("one", "A", buffer_size=0),)),
            lambda: Plan((make_stage("one", "A"),), (Reservation("missing", ("A",)),)),
            lambda: Plan((make_stage("one", "A"),), (Reservation("one", ("B",)),)),
            lambda: Plan((make_stage("one", "A"),), (Reservation("one", ("A",), on_unserved="unknown"),)),
            lambda: Plan((make_stage("one", "A"),), (Reservation("one", ("A",), yield_from=("missing",)),)),
            lambda: Plan((make_stage("one", "A"),), (Reservation("one", ("A",)), Reservation("one", ("A",)))),
        )
        for index, factory in enumerate(bad_plan_factories):
            with self.subTest(case=index):
                with self.assertRaises(PlanError):
                    Pager(factory())

    def test_existing_unpaged_core_oracle_in_30_cases(self):
        rng = random.Random(196)
        cases = 0
        for dataset in range(30):
            rows = [(rng.randrange(8), f"id{i:02d}", 1) for i in range(20)]
            sources = [rng.sample(rows, rng.randrange(21)) for _ in range(3)]
            quotas = [rng.randrange(8) for _ in sources]
            plan = Plan(tuple(Stage(f"stage{i}", tuple(Item(item_id, key) for key, item_id, _ in sorted(source, reverse=True)), quota)
                              for i, (source, quota) in enumerate(zip(sources, quotas))))
            expected_ids = unpaged_staged_oracle(sources, quotas, anchor=1)
            actual, _ = self.traverse(plan, sizes=(1, 50, 3), batches=(1, 4), budgets=(1, 7))
            self.assertEqual([item_id for _, item_id in actual], expected_ids, dataset)
            self.assertEqual(actual, unpaged_oracle(plan), dataset)
            cases += 1
        self.assertEqual(cases, 30)

    def test_existing_prepared_buffer_model_in_30_cases(self):
        rng = random.Random(833)
        cases = 0
        for dataset in range(30):
            source = tuple(Item(f"id{i:02d}", 100 - i) for i in range(rng.randrange(1, 30)))
            ids = [item.id for item in source]
            prior = set(rng.sample(ids, rng.randrange(len(ids) + 1)))
            quota = rng.randrange(10)
            buffer_size = rng.choice((1, 2, 7))
            expected, _, _, _ = prepared_buffer_model(ids, prior, quota, (1, 4, 2), 3, buffer_size)
            plan = Plan((Stage("prior", tuple(item for item in source if item.id in prior)),
                         Stage("prepared", source, quota=quota, buffer_size=buffer_size)))
            actual, _ = self.traverse(plan, sizes=(1, 4, 2), batches=(1, 5), budgets=(3,))
            self.assertEqual([item_id for name, item_id in actual if name == "prepared"], expected, dataset)
            self.assertEqual(actual, unpaged_oracle(plan), dataset)
            cases += 1
        self.assertEqual(cases, 30)

    def test_seeded_reservation_matrix_in_1080_traversals(self):
        rng = random.Random(20261119)
        comparisons = 0
        for dataset in range(40):
            universe = tuple(Item(f"id{i:02d}", rng.randrange(-3, 7)) for i in range(rng.randrange(1, 25)))
            stages = []
            for index in range(rng.randrange(2, 6)):
                chosen = rng.sample(universe, rng.randrange(len(universe) + 1))
                source = tuple(sorted(chosen, key=lambda item: item.position, reverse=True))
                stages.append(Stage(f"s{index}", source, quota=rng.choice((None, 0, 1, 3, 8)),
                                    buffer_size=rng.choice((None, None, 1, 2, 5))))
            claims = []
            for stage in stages:
                if rng.random() < 0.7:
                    reserved = tuple(item.id for item in rng.sample(list(stage.items), rng.randrange(len(stage.items) + 1)))
                    others = [other.name for other in stages if other.name != stage.name]
                    yielding = None if rng.random() < 0.5 else tuple(rng.sample(others, rng.randrange(len(others) + 1)))
                    claims.append(Reservation(stage.name, reserved, yield_from=yielding,
                                              on_unserved=rng.choice(("omit", "release"))))
            rng.shuffle(claims)
            plan = Plan(tuple(stages), tuple(claims), scope=f"seeded-{dataset}")
            expected = unpaged_oracle(plan)
            for sizes in ((1,), (50,), (1, 7, 2, 3)):
                for batches in ((1,), (50,), (2, 5, 1)):
                    for budgets in ((1,), (1000,), (1, 7, 2)):
                        with self.subTest(dataset=dataset, sizes=sizes, batches=batches, budgets=budgets):
                            actual, _ = self.traverse(plan, sizes=sizes, batches=batches, budgets=budgets)
                            self.assertEqual(actual, expected)
                        comparisons += 1
        self.assertEqual(comparisons, 1080)


if __name__ == "__main__":
    unittest.main(verbosity=2)
