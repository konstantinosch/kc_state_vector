"""Grouped state boundaries, response capacity, source rollback and negative controls."""

import copy
from dataclasses import replace
import unittest

from reference.model import Batch, CapacityError, MemorySource, ReadError, Reservation, StateError
from reference.grouped.coverage import expand_coverage
from reference.grouped.model import Pager, Plan, Record, Stage
from reference.grouped.oracle import unpaged_oracle
from reference.grouped.scenarios import targeted_scenarios


class ContractTests(unittest.TestCase):
    def case(self, name):
        return next(case.plan for case in targeted_scenarios() if case.name == name)

    def test_covered_reserved_member_without_own_raw_candidate_resolves(self):
        plan = Plan((Record("A", 3, "g"), Record("B", 2, "g"), Record("C", 1, "g")),
                    (Stage("early", ("A",), quota=1), Stage("owner", ("C",))),
                    (Reservation("owner", ("B",)),))
        expected = [("early", "A", ("A", "C"))]
        # The owner candidate C is already covered: no cursor rewind or replacement
        # representative is manufactured. B is unserved and omitted at completion.
        self.assertEqual(unpaged_oracle(plan), expected)
        for mode in ("ids", "spans"):
            page = Pager(plan, mode).page()
            self.assertEqual(list(page.rows), expected)
            self.assertIsNone(page.state)

    def test_fixed_span_survives_until_last_future_group_candidate(self):
        plan = Plan(tuple(Record(i, k, "g") for i, k in (("A", 3), ("B", 2), ("C", 1))),
                    (Stage("groups", ("A", "B", "C")),))
        pager = Pager(plan, "spans")
        first = pager.page(page_size=1, candidate_budget=1)
        second = pager.page(first.state, page_size=1, candidate_budget=1)
        self.assertEqual(second.rows, ())
        self.assertEqual(first.state["coverage"], second.state["coverage"])
        self.assertEqual(expand_coverage(plan.records, second.state["coverage"], "spans"), set("ABC"))
        final = pager.page(second.state, candidate_budget=1)
        self.assertIsNone(final.state)
        self.assertEqual(final.rows, ())

    def test_plan_and_representation_bind_the_continuation(self):
        plan = self.case("middle_hole_subtract")
        state = Pager(plan).page(page_size=1).state
        variants = (replace(plan, scope="different"),
                    replace(plan, stages=(replace(plan.stages[0], partial_policy="defer"),) + plan.stages[1:]),
                    replace(plan, records=(replace(plan.records[0], episode="other"),) + plan.records[1:]))
        for changed in variants:
            with self.assertRaises(StateError):
                Pager(changed).page(state)
        with self.assertRaises(StateError):
            Pager(plan, "spans").page(state)

    def test_read_failures_leave_incoming_state_retryable(self):
        plan = self.case("middle_hole_subtract")
        for mode in ("ids", "spans"):
            state = Pager(plan, mode).page(page_size=1).state
            before = copy.deepcopy(state)
            healthy = Pager(plan, mode).page(state, batch_size=1)
            for failure in range(1, healthy.reads + 1):
                class Faulty(MemorySource):
                    calls = 0

                    def read(self, stage, cursor, limit):
                        self.calls += 1
                        if self.calls == failure:
                            raise OSError("injected read failure")
                        return super().read(stage, cursor, limit)

                source = Faulty()
                pager = Pager(plan, mode, source=source)
                with self.assertRaises(ReadError):
                    pager.page(state, batch_size=1)
                self.assertEqual(state, before)
                self.assertEqual(pager.page(state, batch_size=1), healthy)
                self.assertEqual(Pager(plan, mode).page(state, batch_size=1), healthy)

    def test_incomplete_prefix_and_false_exhaustion_fail(self):
        plan = self.case("middle_hole_subtract")
        for bad in (Batch((), False), Batch((), True), Batch((), False, False)):
            class BadSource:
                def read(self, stage, cursor, limit):
                    return bad
            with self.assertRaises(ReadError):
                Pager(plan, source=BadSource()).page()

    def test_response_capacity_failure_preserves_input_and_exact_retry(self):
        plan = self.case("middle_hole_subtract")
        for mode in ("ids", "spans"):
            initial = Pager(plan, mode).page(page_size=1)
            before = copy.deepcopy(initial.state)
            healthy = Pager(plan, mode).page(before, page_size=1)
            for limits in ({"max_state_entries": 1}, {"max_state_bytes": 1}):
                with self.assertRaises(CapacityError):
                    Pager(plan, mode, **limits).page(before, page_size=1)
                self.assertEqual(before, initial.state)
                self.assertEqual(Pager(plan, mode).page(before, page_size=1), healthy)

    def test_raw_cursor_pruning_negative_control_repeats_a_member(self):
        plan = self.case("resolver_reachability")

        class WrongPager(Pager):
            def _needed(self, state):
                needed = set(state["pending"])
                for i in range(state["stage"], len(self.plan.stages)):
                    cursor = state["cursors"][i]
                    for identity in self.plan.stages[i].candidate_ids:
                        if cursor is None or self.plan._records[identity].position < tuple(cursor):
                            needed.add(identity)
                return needed

        page = WrongPager(plan, "ids").page()
        self.assertNotEqual(list(page.rows), unpaged_oracle(plan))
        members = [identity for _, _, represented in page.rows for identity in represented]
        self.assertGreater(len(members), len(set(members)))

    def test_naive_range_negative_control_hides_reserved_middle_member(self):
        plan = self.case("middle_hole_subtract")
        state = Pager(plan, "spans").page(page_size=1).state
        wrong = copy.deepcopy(state)
        wrong["coverage"] = [["span", "G", [80, "C"], [100, "A"]]]
        healthy = Pager(plan, "spans").page(state)
        buggy = Pager(plan, "spans").page(wrong)
        self.assertIn(("owner", "B", ("B",)), healthy.rows)
        self.assertNotIn(("owner", "B", ("B",)), buggy.rows)
        # This is a deliberately wrong internally generated vector, not an attempt
        # to validate untrusted state. Logical state cannot authenticate its history.


if __name__ == "__main__":
    unittest.main()
