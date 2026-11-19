"""Closing read-failure and recovered-source retry checks; all inputs are local."""

from unittest import TestCase, main
from unittest.mock import patch

from reference.failure_checks import FAULT_MODES, OneShotFailure, run_failure_checks


class FailureClosureTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = run_failure_checks()

    def test_every_read_has_both_failure_modes_and_both_retry_checks(self):
        summary = self.summary
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["injected_failures"], summary["read_positions"] * len(FAULT_MODES))
        for field in ("fresh_source_retries", "same_source_retries", "complete_recovery_traversals"):
            self.assertEqual(summary[field], summary["injected_failures"])
        self.assertGreater(summary["audited_recovery_boundaries"], summary["injected_failures"])

    def test_sweep_reaches_every_requested_transition_category(self):
        witnesses = self.summary["witness_counts"]
        for name in ("after_owner_quota", "after_owner_exhaustion", "after_owner_zero_quota",
                     "after_release", "after_omit", "after_covered_owner_resolution",
                     "refill_after_same_request_consumption", "refill_after_saved_tail",
                     "after_tentative_selection", "raw_read", "prepared_read"):
            with self.subTest(witness=name):
                self.assertGreater(witnesses[name], 0)

    def test_small_pages_and_fifty_item_requests_both_have_complete_sweeps(self):
        runs = self.summary["runs"]
        self.assertEqual({run["schedule"] for run in runs}, {"wide_50", "crossing_pages"})
        self.assertEqual(self.summary["baseline_traversals"], 2 * self.summary["fixture_count"])
        self.assertTrue(any(run["requests"] > 1 for run in runs if run["schedule"] == "crossing_pages"))
        self.assertTrue(all(run["injected_failures"] == 2 * run["read_positions"] for run in runs))

    def test_disabled_fault_injection_cannot_report_success(self):
        original = OneShotFailure.read

        def bypass(source, stage, cursor, limit):
            source.read_number = 10**6
            return original(source, stage, cursor, limit)

        with patch.object(OneShotFailure, "read", bypass):
            with self.assertRaisesRegex(AssertionError, "failure returned a Page"):
                run_failure_checks()


if __name__ == "__main__":
    main()
