"""Reports separate selected coverage from capacity without changing evidence."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reference.model import Reservation
from reference.grouped.audit import run_comparison
from reference.grouped.model import Plan, Record, Stage
from reference.grouped.reporting import (
    regenerate_findings, render_findings, zero_row_pending_costs,
)
from reference.grouped.scenarios import Scenario


class ReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        plan = Plan((Record("α", 3, "G"), Record("β", 2, "G"), Record("γ", 1, "G")),
                    (Stage("early", ("α", "γ"), partial_policy="defer"), Stage("owner", ("β",))),
                    (Reservation("owner", ("β",)),), scope="long_unicode_pending")
        cls.case = Scenario("long_unicode_pending", "Actual Unicode pending-list bytes.", plan)
        result = run_comparison(plan, 50, 1, 1, representations=("ids", "spans", "hybrid"))
        result.update(name=cls.case.name, kind="long", scoped_records=3, plan_fingerprint=plan.fingerprint,
                      hybrid_larger_coverage_boundaries={"ids": 0, "spans": 0})
        cls.report = {
            "recorded_at_utc": "unchanged-recording-time", "source_sha256": {"model.py": "unchanged-hash"},
            "config": {"groups": 2, "members": 4, "seed": 7},
            "totals": {"three_way_comparisons": 1, "individual_traversals": 3,
                       "audited_boundaries": result["boundaries_audited"],
                       "replay_calls": result["requests"] * 3, "all_oracles_match": True,
                       "previous_two_mode_comparisons_unchanged": 1,
                       "hybrid_larger_coverage_boundaries": {"ids": 0, "spans": 0}},
            "results": [result],
        }

    def reconstructed_plans(self):
        return patch("reference.grouped.reporting.distribution_scenarios",
                     return_value=(self.case,))

    def test_coverage_leads_capacity_and_rendering_keeps_the_report_intact(self):
        before = copy.deepcopy(self.report)
        with self.reconstructed_plans():
            rendered = render_findings(self.report)
        self.assertLess(rendered.index("## Selected coverage"), rendered.index("## Complete-continuation capacity"))
        self.assertIn("request / cumulative selected rows / full pages", rendered)
        self.assertIn("### Zero-row peaks explained at the same boundary", rendered)
        self.assertEqual(self.report, before)

    def test_pending_bytes_come_from_actual_escaped_ids_not_a_per_id_estimate(self):
        with self.reconstructed_plans():
            costs = zero_row_pending_costs(self.report)
        self.assertEqual(len(costs), 1)
        expected = len(json.dumps(["β"], ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        self.assertEqual(costs[0]["pending_members"], 1)
        self.assertEqual(costs[0]["pending_bytes"], expected)
        self.assertNotEqual(costs[0]["pending_bytes"], 13)
        self.assertEqual(costs[0]["page"]["rows_so_far"], 0)

    def test_missing_proof_omits_reconstructed_byte_claims(self):
        for change in ("fingerprint", "count", "frontier"):
            modified = copy.deepcopy(self.report)
            result = modified["results"][0]
            if change == "fingerprint":
                result["plan_fingerprint"] = "different-plan"
            elif change == "count":
                result["pages"][0]["hybrid"]["pending_members"] += 1
            else:
                result["pages"][0]["next_stage"] = 1
            with self.subTest(change=change), self.reconstructed_plans():
                self.assertEqual(zero_row_pending_costs(modified), [])

    def test_prose_regeneration_preserves_saved_json_csv_and_recording_metadata(self):
        with tempfile.TemporaryDirectory() as folder, self.reconstructed_plans():
            root = Path(folder)
            evidence = json.dumps(self.report, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
            csv = b"preserved,evidence\r\nwith,its-original-newlines\r\n"
            (root / "comparison.json").write_bytes(evidence)
            (root / "pages.csv").write_bytes(csv)
            (root / "FINDINGS.md").write_text("old layout", encoding="utf-8")
            target = regenerate_findings(root)
            self.assertEqual(target, root / "FINDINGS.md")
            self.assertIn("Reporting revision:", target.read_text(encoding="utf-8"))
            self.assertEqual((root / "comparison.json").read_bytes(), evidence)
            self.assertEqual((root / "pages.csv").read_bytes(), csv)

    def test_render_only_cli_never_runs_or_rewrites_measurements(self):
        from reference.grouped import run_hybrid_checks
        with patch("sys.argv", ["run_hybrid_checks", "--render-only"]), \
                patch.object(run_hybrid_checks, "run") as run, \
                patch.object(run_hybrid_checks, "write_results") as write, \
                patch.object(run_hybrid_checks, "regenerate_findings", return_value=Path("FINDINGS.md")) as render, \
                patch("builtins.print"):
            run_hybrid_checks.main()
        run.assert_not_called()
        write.assert_not_called()
        render.assert_called_once()


if __name__ == "__main__":
    unittest.main()
