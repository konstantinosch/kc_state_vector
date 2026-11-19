"""Long, ID-only traversals; test reports use a temporary directory.

Run this test alone with:
    python -m unittest reference.tests.test_long_traversals -v

Unlike the earlier small-plan matrix, each traversal selects at least 1,000 IDs
and must include successive full pages of 50. Test reports do not modify the checkout.
"""

from pathlib import Path
import tempfile
import unittest

from reference.long_scenarios import make_scenarios
from reference.long_traversals import (
    SCHEDULES, assert_long_result, run_scenario, write_reports,
)


class LongTraversalTests(unittest.TestCase):
    def test_long_distribution_matrix_with_page_boundary_retirement(self):
        results = []
        scenarios = make_scenarios(size=3000, seed=20261119)
        self.assertEqual(len(scenarios), 13)
        self.assertEqual(len({scenario.name for scenario in scenarios}), len(scenarios))
        for scenario in scenarios:
            for schedule in SCHEDULES:
                with self.subTest(scenario=scenario.name, schedule=schedule.name):
                    result = run_scenario(scenario, schedule)
                    assert_long_result(result)
                    if scenario.name == "core_growth_control" and schedule.name == "wide":
                        self.assertEqual(result["peak_returned_exclusions"], 1500)
                        self.assertEqual(result["peak_exclusions_page"], 30)
                    results.append(result)
                    print(f"PASS {scenario.name}/{schedule.name}: {result['requests']} requests, "
                          f"{result['full_pages']} full pages; peak returned entries "
                          f"{result['peak_returned_entries']} at page {result['peak_entries_page']}", flush=True)
        self.assertEqual(len(results), 39)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            write_reports(output, results, size=3000, seed=20261119)
            self.assertTrue((output / "results.json").is_file())


if __name__ == "__main__":
    unittest.main()
