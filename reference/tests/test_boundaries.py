"""Small regressions found by independent implementation review."""

import copy
import unittest

from reference import Batch, Item, Pager, Plan, ReadError, Stage, StateError


class BoundaryTests(unittest.TestCase):
    def test_zero_quota_nonowner_does_not_force_unneeded_coverage(self):
        a, b = Item("A", 2), Item("B", 1)
        plan = Plan((Stage("first", (a,), 1), Stage("middle", (b,), 1),
                     Stage("disabled", (a,), 0)))
        pager = Pager(plan, max_state_entries=0)
        first = pager.page(page_size=1)
        self.assertEqual(first.ids, ("A",))
        self.assertEqual(first.state["coverage"], [])
        last = pager.page(first.state)
        self.assertEqual(last.ids, ("B",))
        self.assertIsNone(last.state)

    def test_malformed_and_failing_batch_iterables_are_read_errors(self):
        item = Item("A", 1)
        plan = Plan((Stage("source", (item,)),))

        def failing_items():
            yield item
            raise RuntimeError("read stopped while materializing candidates")

        class BrokenSource:
            def __init__(self, value):
                self.value = value

            def read(self, stage, cursor, limit):
                return Batch(self.value, True)

        for value in (None, failing_items()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ReadError):
                    Pager(plan, source=BrokenSource(value)).page()
        self.assertEqual(Pager(plan).page().ids, ("A",))

    def test_nonfinite_state_is_rejected_without_mutating_input(self):
        plan = Plan((Stage("source", (Item("A", 2), Item("B", 1))),))
        pager = Pager(plan)
        first = pager.page(page_size=1)
        incoming = copy.deepcopy(first.state)
        incoming["selected"][0] = float("inf")
        before = copy.deepcopy(incoming)
        with self.assertRaises(StateError):
            pager.page(incoming)
        self.assertEqual(incoming, before)


if __name__ == "__main__":
    unittest.main()
