"""Mutation checks demonstrate that boundary auditing observes retirement errors."""

import copy
import unittest

from reference.model import Item, Pager, Plan, Reservation, Stage
from reference.retirement import audit_boundary


def records():
    return {identity: Item(identity, 5 - index) for index, identity in enumerate("ABCDE")}


class RetirementAuditTests(unittest.TestCase):
    def prepared_boundary(self):
        values = records()
        plan = Plan((Stage("priority", (values["C"],), 1),
                     Stage("rest", tuple(values.values()), buffer_size=4)))
        pager = Pager(plan)
        first = pager.page(page_size=1)
        second = pager.page(first.state, page_size=1)
        rows = first.rows + second.rows
        audit_boundary(plan, second.state, rows)
        return plan, second.state, rows

    def test_valid_boundaries_across_ownership_and_prepared_pages(self):
        values = records()
        for policy in ("omit", "release"):
            plan = Plan((Stage("before", tuple(values.values()), quota=2),
                         Stage("owner", (values["A"], values["D"]), quota=1, buffer_size=2),
                         Stage("rest", tuple(values.values()), buffer_size=3)),
                        (Reservation("owner", ("A", "D"), on_unserved=policy),))
            for budget in (1, 50):
                with self.subTest(policy=policy, budget=budget):
                    pager, state, rows = Pager(plan), None, []
                    for _ in range(30):
                        page = pager.page(state, page_size=1, candidate_budget=budget)
                        rows.extend(page.rows)
                        audit_boundary(plan, page.state, rows)
                        if page.state is None:
                            break
                        state = page.state
                    else:
                        self.fail("finite fixture did not terminate")

    def test_required_coverage_cannot_retire_at_fetch_cursor(self):
        plan, state, rows = self.prepared_boundary()
        self.assertEqual(state["coverage"], ["C"])
        self.assertEqual(state["buffers"][1]["items"], ["B", "C", "D"])
        changed = copy.deepcopy(state)
        changed["coverage"] = []
        with self.assertRaisesRegex(AssertionError, "coverage: missing"):
            audit_boundary(plan, changed, rows)

    def test_unreachable_selected_coverage_must_retire(self):
        plan, state, rows = self.prepared_boundary()
        changed = copy.deepcopy(state)
        changed["coverage"].append("A")
        with self.assertRaisesRegex(AssertionError, "coverage:.*stale"):
            audit_boundary(plan, changed, rows)

    def test_earlier_cursor_cannot_discard_pending_ownership(self):
        values = records()
        plan = Plan((Stage("before", tuple(values.values())),
                     Stage("owner", (values["B"],))), (Reservation("owner", ("B",)),))
        page = Pager(plan).page(page_size=50, candidate_budget=2)
        self.assertEqual(page.state["cursors"][0], list(values["B"].position))
        audit_boundary(plan, page.state, page.rows)
        changed = copy.deepcopy(page.state)
        changed["pending"] = []
        with self.assertRaisesRegex(AssertionError, "pending: missing"):
            audit_boundary(plan, changed, page.rows)

    def test_zero_quota_owner_keeps_covered_resolution_dependency(self):
        values = records()
        plan = Plan((Stage("before", (values["A"],), quota=1),
                     Stage("middle", (values["B"],)),
                     Stage("owner", (values["A"],), quota=0),
                     Stage("rest", (values["C"],))),
                    (Reservation("owner", ("A",), yield_from=("rest",)),))
        page = Pager(plan).page(page_size=1)
        self.assertEqual(page.state["coverage"], ["A"])
        audit_boundary(plan, page.state, page.rows)
        changed = copy.deepcopy(page.state)
        changed["coverage"] = []
        with self.assertRaisesRegex(AssertionError, "coverage: missing"):
            audit_boundary(plan, changed, page.rows)

    def test_omission_and_release_have_distinct_retained_sets(self):
        values = records()
        for policy in ("omit", "release"):
            with self.subTest(policy=policy):
                plan = Plan((Stage("before", (values["A"],), quota=1),
                             Stage("owner", (values["B"],), quota=0),
                             Stage("rest", tuple(values.values()))),
                            (Reservation("owner", ("B",), on_unserved=policy),))
                page = Pager(plan).page(page_size=1)
                audit_boundary(plan, page.state, page.rows)
                changed = copy.deepcopy(page.state)
                changed["omitted"] = [] if policy == "omit" else ["B"]
                with self.assertRaisesRegex(AssertionError, "omitted:"):
                    audit_boundary(plan, changed, page.rows)

    def test_missing_unread_buffer_entry_is_detected(self):
        plan, state, rows = self.prepared_boundary()
        changed = copy.deepcopy(state)
        changed["buffers"][1]["items"].remove("C")
        with self.assertRaisesRegex(AssertionError, "buffer: unread source interval"):
            audit_boundary(plan, changed, rows)

    def test_selection_counts_must_match_history(self):
        plan, state, rows = self.prepared_boundary()
        changed = copy.deepcopy(state)
        changed["selected"][1] += 1
        with self.assertRaisesRegex(AssertionError, "selected counts"):
            audit_boundary(plan, changed, rows)

    def test_terminal_history_cannot_end_early(self):
        plan, _, rows = self.prepared_boundary()
        with self.assertRaisesRegex(AssertionError, "terminal history"):
            audit_boundary(plan, None, rows)


if __name__ == "__main__":
    unittest.main()
