"""Independent mixed-coverage checks and three-representation traversals."""

from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from reference.grouped.audit import audit_boundary, run_comparison
from reference.grouped.generated import generated_scenarios
from reference.grouped.model import Pager, Plan, Record, Stage
from reference.grouped.scenarios import targeted_scenarios


MODES = ("ids", "spans", "hybrid")


def long_group_plan():
    records = tuple(Record(f"record-{index:03d}", index, "G") for index in range(12))
    return Plan(records, (
        Stage("first", (records[-1].id,), quota=1),
        Stage("rest", tuple(record.id for record in records), mode="items"),
    ))


class HybridAuditTests(unittest.TestCase):
    def test_all_three_targeted_and_seeded_schedules_match(self):
        cases = tuple(targeted_scenarios()) + tuple(generated_scenarios(24))
        for case in cases:
            for limits in ((1, 3, 1), (2, 1, 3), (50, 50, 1000)):
                with self.subTest(case=case.name, limits=limits):
                    result = run_comparison(case.plan, *limits, representations=MODES)
                    self.assertTrue(result["oracle_match"])
                    self.assertTrue(result["replay_checked"])
                    self.assertEqual(result["boundaries_audited"], result["requests"] * 3)
                    self.assertEqual(result["representations"], list(MODES))
                    self.assertEqual(json.loads(json.dumps(result)), result)
                    for page in result["pages"]:
                        self.assertEqual(page["rows_so_far"], page["rows_before"] + page["rows"])
                        self.assertEqual(page["hybrid"]["state_overhead_bytes_vs_ids"],
                                         0 if page["next_stage"] is None else 3)

    def test_default_modes_are_unchanged_and_optional_order_is_respected(self):
        plan = long_group_plan()
        default = run_comparison(plan, page_size=1, candidate_budget=1)
        self.assertEqual(default["representations"], ["ids", "spans"])
        self.assertEqual(set(default["modes"]), {"ids", "spans"})
        ordered = run_comparison(plan, page_size=1, candidate_budget=1,
                                 representations=("hybrid", "ids"))
        self.assertEqual(ordered["representations"], ["hybrid", "ids"])
        for field in ("rows", "represented_members", "requests", "consumed", "fetched", "reads"):
            self.assertEqual(default[field], ordered[field])
        single = run_comparison(plan, representations=("hybrid",))
        self.assertEqual(single["boundaries_audited"], single["requests"])
        self.assertIsNone(single["pages"][0]["hybrid"]["state_overhead_bytes_vs_ids"])

    def test_invalid_comparison_modes_are_rejected(self):
        for modes in (None, (), [], "ids", ("ids", "ids"), ("unknown",), (None,)):
            with self.subTest(modes=modes):
                with self.assertRaisesRegex(AssertionError, "representations"):
                    run_comparison(long_group_plan(), representations=modes)

    def test_hybrid_span_cannot_bridge_reserved_hole(self):
        plan = next(case.plan for case in targeted_scenarios() if case.name == "middle_hole_subtract")
        page = Pager(plan, "hybrid").page(page_size=1, candidate_budget=1)
        self.assertEqual(page.rows, (("early", "A", ("A", "C")),))
        self.assertEqual(set(page.state["coverage"]), {"A", "C"})
        damaged = deepcopy(page.state)
        damaged["coverage"] = [["span", "G", [80, "C"], [100, "A"]]]
        with self.assertRaisesRegex(AssertionError, "unselected member"):
            audit_boundary(plan, damaged, page.rows)

    def test_hybrid_mixed_overlap_and_tagged_ids_are_rejected(self):
        plan = long_group_plan()
        page = Pager(plan, "hybrid").page(page_size=1, candidate_budget=1)
        self.assertEqual(len(page.state["coverage"]), 1)
        self.assertEqual(page.state["coverage"][0][0], "span")
        damaged = deepcopy(page.state)
        damaged["coverage"].append("record-000")
        with self.assertRaisesRegex(AssertionError, "overlapping descriptors"):
            audit_boundary(plan, damaged, page.rows)
        damaged["coverage"] = [["id", "record-000"]]
        with self.assertRaisesRegex(AssertionError, "unknown tag"):
            audit_boundary(plan, damaged, page.rows)

    def test_hybrid_bare_ids_retire_individually(self):
        plan = Plan(tuple(Record(identity, 30 - index * 10, identity)
                          for index, identity in enumerate("ABC")), (
            Stage("first", ("A", "B"), quota=2, mode="items"),
            Stage("rest", tuple("ABC"), mode="items"),
        ))
        pager = Pager(plan, "hybrid")
        first = pager.page(page_size=2, candidate_budget=2)
        self.assertEqual(set(first.state["coverage"]), {"A", "B"})
        second = pager.page(first.state, page_size=2, candidate_budget=1)
        self.assertEqual(second.rows, ())
        self.assertEqual(second.state["coverage"], ["B"])
        audit_boundary(plan, second.state, first.rows)
        damaged = deepcopy(second.state)
        damaged["coverage"].append("A")
        with self.assertRaisesRegex(AssertionError, "no remaining consumer"):
            audit_boundary(plan, damaged, first.rows)

    def test_fixed_hybrid_span_can_outlive_the_point_where_ids_are_cheaper(self):
        result = run_comparison(long_group_plan(), page_size=1, candidate_budget=1, representations=MODES)
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["represented_members"], 12)
        self.assertEqual(result["requests"], 13)
        self.assertEqual(result["full_pages"], 1)
        first, last_live = result["pages"][0], result["pages"][-2]
        self.assertEqual(first["hybrid"]["span_entries"], 1)
        self.assertEqual(first["hybrid"]["id_entries"], 0)
        self.assertLess(first["hybrid"]["coverage_bytes"], first["ids"]["coverage_bytes"])
        self.assertEqual(last_live["hybrid"]["coverage_members"], 12)
        self.assertEqual(last_live["hybrid"]["live_selected_members"], 1)
        self.assertEqual(last_live["ids"]["coverage_members"], 1)
        self.assertGreater(last_live["hybrid"]["coverage_bytes"], last_live["ids"]["coverage_bytes"])
        self.assertGreater(last_live["hybrid"]["state_bytes"], last_live["ids"]["state_bytes"])
        self.assertEqual(result["modes"]["hybrid"]["retirement_pages"], [13])
        for mode in MODES:
            metrics = result["modes"][mode]
            for kind in ("state", "coverage"):
                peak_page = result["pages"][metrics[f"peak_{kind}_bytes_page"] - 1]
                self.assertEqual(metrics[f"peak_{kind}_bytes"], peak_page[mode][f"{kind}_bytes"])
                self.assertEqual(metrics[f"peak_{kind}_bytes_rows"], peak_page["rows_so_far"])
                self.assertEqual(metrics[f"peak_{kind}_bytes_full_pages"], peak_page["full_pages_so_far"])
            self.assertEqual(metrics["peak_state_bytes_rows"], 1)

    def test_replacing_live_hybrid_span_with_exact_ids_is_rejected(self):
        plan = long_group_plan()
        pager = Pager(plan, "hybrid")
        first = pager.page(page_size=1, candidate_budget=1)
        second = pager.page(first.state, page_size=1, candidate_budget=1)
        changed = deepcopy(second.state)
        changed["coverage"] = [f"record-{index:03d}" for index in range(11)]
        # This replacement is independently exact on the live set. Static
        # descriptor admission still forbids replacing a span before retirement.
        audit_boundary(plan, changed, first.rows)
        original_page = Pager.page

        def replacing_page(instance, state=None, **limits):
            page = original_page(instance, state=state, **limits)
            if (instance.representation == "hybrid" and page.state is not None
                    and page.state["cursors"][1] == [11, "record-011"]):
                altered = deepcopy(page.state)
                altered["coverage"] = [f"record-{index:03d}" for index in range(11)]
                return replace(page, state=altered)
            return page

        with patch.object(Pager, "page", new=replacing_page):
            with self.assertRaisesRegex(AssertionError, "still-needed fixed descriptor"):
                run_comparison(plan, page_size=1, candidate_budget=1, representations=MODES)


if __name__ == "__main__":
    unittest.main()
