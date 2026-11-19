"""Local reports cannot upgrade smoke runs or failed reruns to exhaustive passes."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reference.exhaustive import run_family, summarize
from reference.tiny_plans import TinyCase, iter_cases


class ExhaustiveReportingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)

    def test_smoke_run_never_claims_exhaustive_even_if_limit_exceeds_domain(self):
        # Replace the count and iterator with a complete one-case test domain;
        # explicitly requesting --limit still labels the run partial.
        case = next(iter_cases("core"))
        with patch("reference.exhaustive.iter_cases", return_value=iter((case,))), \
             patch.dict("reference.exhaustive.EXPECTED_COUNTS", {"core": 1}), \
             contextlib.redirect_stdout(io.StringIO()):
            result = run_family("core", self.output, limit=5)
        self.assertFalse(result["exhaustive"])
        self.assertEqual(result["total_traversals"], 3)
        self.assertEqual(result["total_audited_boundaries"], 3)

    def test_failure_preserves_replay_fixture_and_invalidates_previous_pass(self):
        (self.output / "core.json").write_text('{"exhaustive": true}')
        with patch("reference.exhaustive.validate_case", side_effect=AssertionError("injected defect")):
            with self.assertRaisesRegex(AssertionError, "case 1, raw_step"):
                run_family("core", self.output, limit=1)
        artifact = json.loads((self.output / "failure-core.json").read_text())
        case = TinyCase.from_dict(artifact["case"])
        self.assertEqual(case, next(iter_cases("core")))
        self.assertEqual(case.to_plan().stages[0].items, ())
        self.assertEqual(artifact["profile"], "raw_step")
        self.assertIn("injected defect", artifact["failure"])
        report = json.loads((self.output / "core.json").read_text())
        self.assertFalse(report["exhaustive"])
        self.assertEqual(report["status"], "failed")

    def test_truncated_unlimited_enumeration_cannot_pass(self):
        with patch("reference.exhaustive.iter_cases", return_value=iter(())):
            with self.assertRaisesRegex(AssertionError, "enumeration count"):
                run_family("core", self.output)
        report = json.loads((self.output / "core.json").read_text())
        self.assertFalse(report["exhaustive"])

    def test_summary_rejects_partial_family(self):
        for family in ("core", "single_owner", "conflicting_owners"):
            (self.output / f"{family}.json").write_text(json.dumps({"family": family, "exhaustive": False}))
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            summarize(self.output)


if __name__ == "__main__":
    unittest.main()
