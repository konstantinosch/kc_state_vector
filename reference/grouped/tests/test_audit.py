"""Independent retirement-audit controls and small paired traversal matrix."""

from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from reference.model import Reservation
from reference.grouped.audit import audit_boundary, run_comparison
from reference.grouped.generated import generated_scenarios
from reference.grouped.model import Pager, Plan, Record, Stage
from reference.grouped.scenarios import targeted_scenarios


class GroupedAuditTests(unittest.TestCase):
    def scenario(self, name):
        return next(case.plan for case in targeted_scenarios() if case.name == name)

    def test_missing_required_coverage_is_rejected(self):
        plan = self.scenario("resolver_reachability")
        for mode in ("ids", "spans"):
            with self.subTest(mode=mode):
                page = Pager(plan, mode).page(page_size=1, candidate_budget=1)
                audit_boundary(plan, page.state, page.rows)
                damaged = deepcopy(page.state)
                damaged["coverage"] = []
                with self.assertRaisesRegex(AssertionError, "missing selected members"):
                    audit_boundary(plan, damaged, page.rows)

    def test_naive_span_cannot_bridge_a_reserved_hole(self):
        plan = self.scenario("middle_hole_subtract")
        page = Pager(plan, "spans").page(page_size=1, candidate_budget=1)
        self.assertEqual(page.rows, (("early", "A", ("A", "C")),))
        damaged = deepcopy(page.state)
        damaged["coverage"] = [["span", "G", [80, "C"], [100, "A"]]]
        with self.assertRaisesRegex(AssertionError, "unselected member"):
            audit_boundary(plan, damaged, page.rows)

    def test_dropping_pending_claim_is_rejected(self):
        plan = self.scenario("middle_hole_subtract")
        page = Pager(plan).page(page_size=1, candidate_budget=1)
        self.assertEqual(page.state["pending"], ["B"])
        damaged = deepcopy(page.state)
        damaged["pending"] = []
        with self.assertRaisesRegex(AssertionError, "pending: missing"):
            audit_boundary(plan, damaged, page.rows)

    def test_deferred_consumed_owner_candidate_remains_pending(self):
        records = tuple(Record(identity, 40 - index * 10, "G")
                        for index, identity in enumerate("ABCD"))
        for completion in ("release", "omit"):
            with self.subTest(completion=completion):
                plan = Plan(records, (
                    Stage("first_owner", ("B", "C"), partial_policy="defer"),
                    Stage("later_owner", ("D",), partial_policy="defer"),
                    Stage("rest", tuple("ABCD")),
                ), (
                    Reservation("first_owner", ("B", "C"), on_unserved=completion),
                    Reservation("later_owner", ("C", "D")),
                ))
                page = Pager(plan).page(candidate_budget=1)
                self.assertEqual(page.rows, ())
                self.assertEqual(page.state["cursors"][0], [30, "B"])
                self.assertEqual(page.state["pending"], ["B", "C", "D"])
                audit_boundary(plan, page.state, ())
                damaged = deepcopy(page.state)
                damaged["pending"].remove("B")
                with self.assertRaisesRegex(AssertionError, "pending: missing"):
                    audit_boundary(plan, damaged, ())
                result = run_comparison(plan, candidate_budget=1)
                self.assertEqual(result["represented_members"], 4 if completion == "release" else 2)

    def test_group_only_owner_member_is_not_a_consumed_raw_candidate(self):
        plan = Plan((Record("A", 40, "G"), Record("B", 30, "G"),
                     Record("C", 20, "G"), Record("D", 10, "H")), (
            Stage("first", ("A",), quota=1, mode="items"),
            Stage("owner", ("B", "D")),
        ), (Reservation("owner", ("A",), yield_from=(), on_unserved="omit"),))
        for mode in ("ids", "spans"):
            with self.subTest(mode=mode):
                pager = Pager(plan, mode)
                first = pager.page(page_size=1, candidate_budget=1)
                second = pager.page(first.state, page_size=1, candidate_budget=1)
                self.assertEqual(second.rows, (("owner", "B", ("B", "C")),))
                self.assertEqual(second.state["pending"], ["A"])
                self.assertEqual(second.state["cursors"][1], [30, "B"])
                # The remaining raw candidate D reaches only H. A's coverage is
                # retained solely to distinguish covered from unserved at finish.
                report = audit_boundary(plan, second.state, first.rows + second.rows)
                self.assertEqual(report["live_selected_members"], 1)
                damaged = deepcopy(second.state)
                damaged["pending"] = []
                with self.assertRaisesRegex(AssertionError, "pending: missing"):
                    audit_boundary(plan, damaged, first.rows + second.rows)
        self.assertEqual(run_comparison(plan, page_size=1, candidate_budget=1)["represented_members"], 4)

    def test_wholly_dead_span_is_rejected(self):
        plan = Plan((Record("A", 30, "G"), Record("B", 20, "G"), Record("X", 10, "H")), (
            Stage("first", ("A",), quota=1), Stage("rest", ("X",), mode="items"),
        ))
        page = Pager(plan, "spans").page(page_size=1, candidate_budget=1)
        self.assertEqual(page.state["coverage"], [])
        damaged = deepcopy(page.state)
        damaged["coverage"] = [["span", "G", [20, "B"], [30, "A"]]]
        with self.assertRaisesRegex(AssertionError, "no remaining consumer"):
            audit_boundary(plan, damaged, page.rows)

    def test_fixed_span_can_retain_dead_selected_members(self):
        plan = Plan(tuple(Record(identity, 30 - index * 10, "G")
                          for index, identity in enumerate("ABC")), (
            Stage("first", ("A",), quota=1), Stage("rest", tuple("ABC"), mode="items"),
        ))
        reports = {}
        for mode in ("ids", "spans"):
            pager = Pager(plan, mode)
            first = pager.page(page_size=1, candidate_budget=1)
            second = pager.page(first.state, page_size=1, candidate_budget=1)
            self.assertEqual(second.rows, ())
            reports[mode] = audit_boundary(plan, second.state, first.rows)
            self.assertEqual(reports[mode]["live_selected_members"], 2)
        self.assertEqual(reports["ids"]["coverage_members"], 2)
        self.assertEqual(reports["spans"]["coverage_members"], 3)
        result = run_comparison(plan, page_size=1, candidate_budget=1)
        self.assertEqual(result["modes"]["ids"]["retired_entries"], 3)
        self.assertEqual(result["modes"]["spans"]["retired_entries"], 1)
        self.assertEqual(result["modes"]["spans"]["retirement_pages"], [4])

    def test_live_set_exact_span_shrink_violates_fixed_descriptor_contract(self):
        plan = Plan(tuple(Record(identity, 30 - index * 10, "G")
                          for index, identity in enumerate("ABC")), (
            Stage("first", ("A",), quota=1), Stage("rest", tuple("ABC"), mode="items"),
        ))
        pager = Pager(plan, "spans")
        first = pager.page(page_size=1, candidate_budget=1)
        second = pager.page(first.state, page_size=1, candidate_budget=1)
        shortened = deepcopy(second.state)
        shortened["coverage"] = [["span", "G", [10, "C"], [20, "B"]]]
        # B,C are exactly the remaining needed members. The shortened span is
        # individually valid and exact; only the transition violates fixed bounds.
        report = audit_boundary(plan, shortened, first.rows)
        self.assertEqual(report["coverage_members"], report["live_selected_members"])
        self.assertEqual(report["live_selected_members"], 2)

        original_page = Pager.page

        def shrinking_page(instance, state=None, **limits):
            page = original_page(instance, state=state, **limits)
            if (instance.representation == "spans" and page.state is not None
                    and page.state["cursors"][1] == [30, "A"]):
                changed = deepcopy(page.state)
                changed["coverage"] = [["span", "G", [10, "C"], [20, "B"]]]
                return replace(page, state=changed)
            return page

        with patch.object(Pager, "page", new=shrinking_page):
            with self.assertRaisesRegex(AssertionError, "still-needed fixed descriptor"):
                run_comparison(plan, page_size=1, candidate_budget=1)

    def test_auditor_does_not_call_codec_or_pager_pruning(self):
        plan = self.scenario("middle_hole_subtract")
        page = Pager(plan, "spans").page(page_size=1, candidate_budget=1)
        with patch("reference.grouped.coverage.expand_coverage",
                   side_effect=AssertionError("must not share the runtime decoder")), \
                patch.object(Pager, "_needed", side_effect=AssertionError("must not share pruning")):
            audit_boundary(plan, page.state, page.rows)

    def test_false_terminal_and_repeated_member_are_rejected(self):
        plan = self.scenario("middle_hole_subtract")
        page = Pager(plan).page(page_size=1, candidate_budget=1)
        with self.assertRaisesRegex(AssertionError, "terminal history"):
            audit_boundary(plan, None, page.rows)
        with self.assertRaisesRegex(AssertionError, "represented twice"):
            audit_boundary(plan, page.state, page.rows + page.rows)

    def test_targeted_and_seeded_plans_match_with_replay(self):
        plans = tuple(targeted_scenarios()) + tuple(generated_scenarios(24))
        for scenario in plans:
            for page_size, batch_size, budget in ((1, 3, 1), (2, 1, 3), (50, 50, 1000)):
                with self.subTest(scenario=scenario.name, limits=(page_size, batch_size, budget)):
                    result = run_comparison(scenario.plan, page_size, batch_size, budget)
                    self.assertTrue(result["oracle_match"])
                    self.assertTrue(result["replay_checked"])
                    self.assertEqual(result["boundaries_audited"], result["requests"] * 2)
                    self.assertEqual(json.loads(json.dumps(result)), result)


if __name__ == "__main__":
    unittest.main()
