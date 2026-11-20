"""Check the placement/recovery measurement harness using bounded plans."""

import unittest
from unittest.mock import patch

from reference.model import Item, Plan, Stage
from reference.placement.adapters import Adapter
from reference.placement import checks


def growth_adapter():
    items = tuple(Item(f"id{i:04d}", i) for i in range(180, 0, -1))
    return Adapter(Plan((Stage("priority", items[::2]), Stage("rest", items))))


class PlacementCheckTests(unittest.TestCase):
    def test_member_differences_detect_all_three_approximation_losses(self):
        expected = [["early", "A", ["A", "B"]], ["rest", "C", ["C"]]]
        actual = [["early", "A", ["A", "B"]], ["best_effort", "B", ["B"]],
                  ["best_effort", "D", ["D"]]]
        result = checks.represented_differences(expected, actual)
        self.assertEqual(result["duplicate_member_occurrences"], 1)
        self.assertEqual(result["omitted_members"], 1)
        self.assertEqual(result["unexpected_members"], 1)
        self.assertFalse(result["same_ordered_members"])

    def test_finite_case_exercises_spill_exact_recovery_and_both_fallbacks(self):
        result = checks.run_case(growth_adapter(), 1024,
                                 {"page_size": 10, "batch_size": 10, "candidate_budget": 1000})
        self.assertTrue(result["all_checks_passed"])
        self.assertGreater(result["healthy"]["spilled_pages"], 0)
        self.assertEqual(result["healthy"]["terminal_pages"], 1)
        self.assertGreater(result["exact_recovery"]["hot_evictions"], 0)
        self.assertEqual(len(result["fallbacks"]), 6)
        self.assertEqual({row["policy"] for row in result["fallbacks"]}, {"best_effort", "restart"})
        self.assertTrue(all(row["declared_result_match"] and row["retry_identical"]
                            for row in result["fallbacks"]))
        for row in result["fallbacks"]:
            if row["policy"] == "restart":
                self.assertEqual(row["client_action"], "replace")
                self.assertEqual(row["omitted_members"], 0)
                self.assertEqual(row["duplicate_member_occurrences"], 0)

    def test_healthy_checker_rejects_changed_work_counters(self):
        from reference.placement.protocol import Service

        original = Service.request
        def broken(service, *args, **kwargs):
            result = original(service, *args, **kwargs)
            result["work"]["reads"] += 1
            return result
        adapter = growth_adapter()
        params = {"page_size": 50, "batch_size": 50, "candidate_budget": 1000}
        baseline = checks.reference_trace(adapter, params)
        with patch.object(Service, 'request', broken), self.assertRaisesRegex(AssertionError, "work counters"):
            checks.healthy_run(adapter, 2048, params, baseline)

    def test_plain_final_stream_best_effort_has_same_remaining_items(self):
        from reference.placement.protocol import Service

        # Give the plain single source a large payload-free scope binding to force
        # a stored vector, while selection itself needs no exclusion history.
        items = tuple(Item(f"id{i:04d}", i) for i in range(30, 0, -1))
        adapter = Adapter(Plan((Stage("plain", items),), scope="scope-" + "x" * 700))
        result = checks.run_case(adapter, 1024, {"page_size": 5}, loss_points=1)
        best = [row for row in result["fallbacks"] if row["policy"] == "best_effort"]
        self.assertTrue(best, "test must actually spill before losing state")
        self.assertTrue(best[0]["same_ordered_members"])
        self.assertEqual(best[0]["duplicate_member_occurrences"], 0)
        self.assertEqual(best[0]["omitted_members"], 0)

    def test_report_separates_terminal_and_nonterminal_fractions(self):
        result = checks.run_case(growth_adapter(), 1024, {"page_size": 50}, loss_points=0)
        result.update(name="growth", description="Small test", grouped=False,
                      representation="ids", scoped_records=180, plan_fingerprint="test")
        m = result["healthy"]
        self.assertEqual(m["requests"], m["inline_pages"] + m["spilled_pages"] + m["terminal_pages"])
        self.assertAlmostEqual(m["inline_fraction_nonterminal"] + m["spilled_fraction_nonterminal"], 1.0)
        terminal = checks.run_case(
            Adapter(Plan((Stage("plain", (Item("only", 1),)),))),
            8192, {"page_size": 50}, loss_points=0)["healthy"]
        self.assertEqual(terminal["requests"], 1)
        self.assertEqual(terminal["terminal_pages"], 1)
        self.assertEqual(terminal["inline_pages"] + terminal["spilled_pages"], 0)
        self.assertIsNone(terminal["inline_fraction_nonterminal"])
        self.assertIsNone(terminal["spilled_fraction_nonterminal"])

    def test_full_page_count_is_not_cumulative_row_equivalents(self):
        adapter = Adapter(Plan((Stage("plain", tuple(Item(str(i), i) for i in range(10))),)))
        result = checks.run_case(adapter, 8192,
                                 {"page_size": 5, "batch_size": 2, "candidate_budget": 2},
                                 loss_points=0)
        self.assertEqual(result["healthy"]["rows"], 10)
        self.assertTrue(all(page["rows"] == 2 for page in result["pages"]))
        self.assertTrue(all(page["full_pages_so_far"] == 0 for page in result["pages"]))


if __name__ == '__main__':
    unittest.main()
