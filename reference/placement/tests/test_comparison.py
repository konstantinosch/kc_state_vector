"""The published numbers compare equal inputs and account for all retained state."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reference.placement import comparison
from reference.placement.scenarios import small_scenarios


def report_for(case, suite='smoke'):
    return {'config': {'suite': suite}, 'source_sha256': {},
            'totals': comparison.summarize_results([case]), 'results': [case]}


class ComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scenario, category = next(iter(comparison.fixtures('smoke').values()))
        cls.case = comparison.compare_case(scenario.adapter, {'page_size': 50}, 2048, True)
        cls.case.update(name=scenario.name, description=scenario.description, category=category, page_size=50)
        tiny = small_scenarios()[0]
        cls.terminal = comparison.compare_case(tiny.adapter, {'page_size': 50}, 8192, True)
        cls.terminal.update(name=tiny.name, description=tiny.description, category='core', page_size=50)

    def test_real_selectors_compare_identical_rows_and_parameters(self):
        case = self.case
        self.assertTrue(case['actual_selector_used'])
        self.assertTrue(case['logical_results_identical'])
        self.assertTrue(case['optional_wire_bytes_identical'])
        self.assertEqual(case['parameters'], {'page_size': 50, 'batch_size': 50, 'candidate_budget': 1000})
        counts = []
        for mode in comparison.MODES:
            strategy = case['strategies'][mode]
            summary = strategy['summary']
            self.assertEqual(summary['rows'], case['oracle_rows'])
            self.assertEqual(summary['selector']['total']['select_calls'], summary['requests'])
            self.assertEqual(strategy['failures']['counts']['fresh_worker_retries'], summary['requests'])
            counts.append(summary['selector'])
        self.assertEqual(counts[0], counts[1])
        self.assertEqual(counts[1], counts[2])

    def test_witness_exercises_spill_return_and_real_retry_cost(self):
        modes = self.case['strategies']
        self.assertTrue(modes['one_way']['summary']['spill_requests'])
        self.assertFalse(modes['one_way']['summary']['return_requests'])
        self.assertTrue(modes['two_way']['summary']['return_requests'])
        self.assertEqual(modes['two_way']['summary']['return_requests'],
                         modes['recompute']['summary']['return_requests'])
        delta = self.case['comparisons']['two_way_to_recompute']
        self.assertGreater(delta['extra_retry']['select_calls'], 0)
        self.assertGreater(delta['extra_retry']['consumed'], 0)
        for mode in comparison.MODES:
            faults = modes[mode]['failures']['counts']
            self.assertGreater(faults['before_commit_failures'], 0)
            self.assertGreater(faults['lost_responses'], 0)
            self.assertGreater(faults['hot_evictions'], 0)

    def test_storage_partition_includes_bridge_and_metadata(self):
        for mode in comparison.MODES:
            strategy = self.case['strategies'][mode]
            for page in strategy['pages']:
                components = ('hot', 'backup', 'head_metadata', 'return_bridge_payload',
                              'other_response_payload', 'redirects')
                self.assertEqual(page['total_bytes'], sum(page[key + '_bytes'] for key in components))
                self.assertEqual(page['response_payload_bytes'],
                                 page['return_bridge_payload_bytes'] + page['other_response_payload_bytes'])
                if page['transition'] != 'handle_to_inline':
                    self.assertEqual(page['return_bridge_payload_bytes'], 0)
            self.assertEqual(strategy['summary']['total_byte_pages'],
                             sum(page['total_bytes'] for page in strategy['pages']))
        self.assertGreater(self.case['strategies']['recompute']['summary']['return_bridge_payload_byte_pages'], 0)

    def test_pairwise_savings_and_costs_include_negative_results(self):
        for old, new in comparison.PAIRS:
            result = self.case['comparisons'][old + '_to_' + new]
            a, b = self.case['strategies'][old], self.case['strategies'][new]
            for name in comparison.STORAGE_COMPONENTS:
                key = name + '_byte_pages'
                self.assertEqual(result['saved'][key], a['summary'][key] - b['summary'][key])
            for key in ('select_calls', 'consumed', 'fetched', 'reads'):
                self.assertEqual(result['extra_retry'][key],
                                 b['failures']['selector']['phases']['retry'][key] -
                                 a['failures']['selector']['phases']['retry'][key])
        self.assertLess(self.case['comparisons']['one_way_to_recompute']['saved']['total_byte_pages'], 0)

    def test_terminal_does_not_inflate_inline_fraction(self):
        totals = comparison.summarize_results([self.case, self.terminal])
        for mode in comparison.MODES:
            values = totals['strategies'][mode]
            self.assertEqual(values['terminal_responses'], 2)
            self.assertEqual(values['single_response_cases'], 1)
            self.assertEqual(values['nonterminal_responses'], values['healthy_requests'] - 2)
            self.assertEqual(values['inline_nonterminal_fraction'],
                             values['inline_nonterminal_responses'] / values['nonterminal_responses'])
            terminal = comparison.summarize_results([self.terminal])['strategies'][mode]
            self.assertEqual(terminal['nonterminal_responses'], 0)
            self.assertIsNone(terminal['inline_nonterminal_fraction'])

    def test_report_is_reproducible_and_preserves_prior_evidence(self):
        report = report_for(self.case)
        with tempfile.TemporaryDirectory() as directory:
            left, right = Path(directory) / 'left', Path(directory) / 'right'
            comparison.write_report(report, left)
            comparison.write_report(deepcopy(report), right)
            for name in ('comparison.json', 'pages.csv', 'FINDINGS.md'):
                self.assertEqual((left / name).read_bytes(), (right / name).read_bytes())
            self.assertEqual(json.loads((left / 'comparison.json').read_text(encoding='utf-8')), report)
            original = (left / 'comparison.json').read_bytes()
            with self.assertRaisesRegex(ValueError, 'never overwritten'):
                comparison.write_report(report, left)
            self.assertEqual(original, (left / 'comparison.json').read_bytes())
        findings = comparison.findings(report)
        self.assertIn('nonterminal responses only', findings)
        self.assertIn('not Python object sizes', findings)
        self.assertIn('One-way byte-pages', findings)
        self.assertIn('100% retry schedule', findings)

    def test_matrix_uses_existing_core_and_grouped_dimensions(self):
        self.assertEqual(len(comparison.schedule('smoke')), 2)
        self.assertEqual(len(comparison.schedule('core')), 12)
        grouped = comparison.fixtures('grouped')
        schedule = comparison.schedule('grouped', grouped)
        self.assertEqual(len(grouped), 24)
        self.assertEqual(len(schedule), 81)
        self.assertTrue(all(len(case.adapter.records) == 2400 for case, _ in grouped.values()))
        small = [spec for spec in schedule if spec[1] in (1, 3, 7)]
        self.assertEqual(len(small), 9)
        self.assertTrue(all(spec[2:] == (4096, True) for spec in small))
        self.assertEqual(len(comparison.schedule('full')), 93)

    def test_source_changes_abort_report(self):
        with patch.object(comparison, 'source_hashes', side_effect=({'a': 'old'}, {'a': 'changed'})), \
                patch.object(comparison, 'fixtures', return_value={}), \
                patch.object(comparison, 'schedule', return_value=[]), \
                patch.object(comparison, '_initialize'):
            with self.assertRaisesRegex(AssertionError, 'source changed'):
                comparison.run('smoke')

    def test_invalid_worker_count_is_rejected(self):
        for workers in (0, -1, True, 1.5):
            with self.assertRaisesRegex(ValueError, 'positive integer'):
                comparison.run(workers=workers)


if __name__ == '__main__':
    unittest.main()
