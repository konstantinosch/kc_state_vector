"""Closing checks for capacity thresholds, ownership and retry correctness."""

import unittest

from reference.capacity_checks import CapacityProbe, dimensions, fixtures, run_capacity_checks


class CapacityClosureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_capacity_checks()

    def test_all_requests_have_exact_fit_and_black_box_threshold_checks(self):
        result = self.result
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["requests_checked"], len(result["request_records"]))
        self.assertEqual(result["exact_fit_checks"], 3 * len(result["request_records"]))
        self.assertGreater(result["black_box_search_attempts"], 0)
        self.assertEqual(result["one_below_failures"], 2 * result["healthy_retry_checks"])

    def test_owner_completion_and_coverage_transitions_are_exercised(self):
        witnesses = self.result["witnesses"]
        for name in ("pending_to_coverage", "omit_completion", "release_completion",
                     "covered_zero_quota", "exhausted_owner", "failure_at_owner_completion"):
            self.assertIsNotNone(witnesses[name], name)
        self.assertGreater(witnesses["covered_zero_quota"]["covered_pending"], 0)
        self.assertGreater(witnesses["covered_zero_quota"]["unserved_pending"], 0)

    def test_prepared_refill_can_exceed_returned_token_requirements(self):
        witness = self.result["witnesses"]["refill_exceeds_returned_size"]
        self.assertEqual(witness["checkpoint"]["phase"], "prepared_refill")
        for dimension in ("entries", "bytes"):
            self.assertGreater(witness["checkpoint"][dimension], witness["returned"][dimension])
            self.assertLessEqual(witness["incoming"][dimension], witness["returned"][dimension])
            self.assertEqual(witness["failed_phases_at_returned_limits"][dimension], "prepared_refill")

    def test_hand_counted_three_item_buffer_peak(self):
        plan = next(value.plan for value in fixtures() if value.name == "prepared_refill")
        probe = CapacityProbe(plan, max_state_entries=None, max_state_bytes=None)
        first = probe.page(page_size=2, batch_size=3, candidate_budget=5)
        self.assertEqual(dimensions(first.state)["entries"], 1)
        second_probe = CapacityProbe(plan, max_state_entries=None, max_state_bytes=None)
        second = second_probe.page(first.state, page_size=2, batch_size=3, candidate_budget=5)
        self.assertEqual(second.ids, ("C", "D"))
        self.assertEqual(dimensions(second.state)["entries"], 2)
        refill = [point for point in second_probe.checkpoints if point["phase"] == "prepared_refill"]
        self.assertEqual(len(refill), 1)
        self.assertEqual(refill[0]["entries"], 3)
        self.assertEqual(refill[0]["state"]["buffers"][0]["items"], ["D", "E", "F"])

    def test_zero_entry_and_terminal_exemptions_are_explicit(self):
        self.assertIsNotNone(self.result["witnesses"]["zero_entry_success"])
        self.assertEqual(self.result["witnesses"]["terminal_zero_limits"]["enforced_peaks"],
                         {"entries": 0, "bytes": 0})

    def test_unicode_uses_declared_compact_json_byte_definition(self):
        witness = self.result["witnesses"]["unicode_bytes"]
        self.assertGreater(witness["canonical_ascii_escaped_bytes"], witness["raw_utf8_json_bytes"])


if __name__ == "__main__":
    unittest.main()
