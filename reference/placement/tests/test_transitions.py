"""Observable fault outcomes, selector-cache isolation and honest coverage counts."""
from copy import deepcopy
import unittest

from reference.grouped.generated import distribution_scenarios
from reference.placement.adapters import Adapter
from reference.placement.checks import run_case
from reference.placement.transition_checks import (
    ValidatedSelection, prepare_baseline, run_transition_case,
)
from reference.placement.transition_checks import calibrated_threshold, summarize
from .test_protocol import growth_adapter, priority_adapter

PARAMS = {'page_size': 3, 'batch_size': 7, 'candidate_budget': 1000}


class TransitionChecksTests(unittest.TestCase):
    def test_selector_rejects_different_state_parameters_and_binding(self):
        adapter = growth_adapter()
        baseline = prepare_baseline(adapter, PARAMS)
        tape = ValidatedSelection(adapter, baseline)
        with self.assertRaisesRegex(AssertionError, 'parameters'):
            tape.select(None, {**PARAMS, 'page_size': 1})
        with self.assertRaisesRegex(AssertionError, 'unvalidated state'):
            tape.select({'unexpected': 1}, PARAMS)
        with self.assertRaisesRegex(AssertionError, 'binding'):
            ValidatedSelection(priority_adapter(), baseline)

    def test_selector_retries_are_copy_isolated(self):
        adapter = growth_adapter()
        baseline = prepare_baseline(adapter, PARAMS)
        tape = ValidatedSelection(adapter, baseline)
        expected = deepcopy(baseline['pages'][0])
        result = tape.select(None, PARAMS)
        result['rows'].clear()
        result['state']['stage'] = 999
        self.assertEqual(tape.select(None, PARAMS), expected)
        self.assertEqual(baseline['pages'][0], expected)

    def test_mid_chain_switch_has_first_spill_retry_and_repeated_handle_eviction(self):
        result = run_transition_case(growth_adapter(), 768, PARAMS)
        self.assertTrue(result['healthy']['mid_chain_switch'])
        self.assertTrue(result['healthy']['multiple_handle_responses'])
        exact = result['exact_recovery']
        self.assertTrue(exact['first_spill_lost_response_replayed'])
        self.assertEqual(exact['hot_evictions'], result['healthy']['handle_requests'])
        self.assertGreaterEqual(exact['handle_lost_responses'], 2)
        self.assertEqual(exact['before_commit_failures'], exact['lost_responses'])
        self.assertEqual({f['policy'] for f in result['fallbacks']}, {'restart', 'best_effort'})
        self.assertEqual(len(result['loss_requests']), 3)
        self.assertTrue(all(f['identical_retries'] == 2 and f['declared_result_match'] for f in result['fallbacks']))

    def test_terminal_control_does_not_claim_transition_or_failures(self):
        result = run_transition_case(priority_adapter(), 8192, {**PARAMS, 'page_size': 50})
        self.assertTrue(result['healthy']['single_response'])
        self.assertFalse(result['healthy']['mid_chain_switch'])
        self.assertFalse(result['healthy']['multiple_handle_responses'])
        self.assertEqual(result['exact_recovery']['lost_responses'], 0)
        self.assertEqual(result['fallbacks'], [])

    def test_memoized_fallbacks_match_direct_pager_in_each_encoding(self):
        plan = next(distribution_scenarios(groups=4, members=12)).plan
        params = {**PARAMS, 'page_size': 1}
        keys = ('policy', 'loss_before_request', 'rows_before_loss', 'post_loss_requests',
                'post_loss_rows', 'duplicate_member_occurrences', 'omitted_members', 'unexpected_members')
        for representation in ('ids', 'spans', 'hybrid'):
            with self.subTest(representation=representation):
                adapter = Adapter(plan, representation)
                direct = run_case(adapter, 512, params)
                cached = run_transition_case(adapter, 512, params)
                self.assertEqual(cached['healthy']['requests'], direct['healthy']['requests'])
                self.assertEqual([{k: f[k] for k in keys} for f in cached['fallbacks']],
                                 [{k: f[k] for k in keys} for f in direct['fallbacks']])

    def test_calibration_requires_growth_and_two_handle_responses(self):
        def probe(values):
            return {'summary': {'initial_token_bytes': 300},
                    'pages': [{'kind': 'terminal' if value == 0 else 'inline', 'token_bytes': value}
                              for value in values]}
        self.assertEqual(calibrated_threshold(probe([1000, 1100, 900, 0])), 1000)
        self.assertIsNone(calibrated_threshold(probe([1000, 900, 800, 0])))
        self.assertIsNone(calibrated_threshold(probe([1000, 900, 1100, 0])))
        self.assertIsNone(calibrated_threshold(probe([0])))

    def test_coverage_counts_keep_first_response_spill_distinct_from_mid_chain(self):
        cases = []
        for name, adapter, threshold, params in (
            ('terminal', priority_adapter(), 8192, {**PARAMS, 'page_size': 50}),
            ('first', growth_adapter(), 512, PARAMS),
            ('middle', growth_adapter(), 768, PARAMS),
        ):
            result = run_transition_case(adapter, threshold, params)
            result.update(name=name, matrix='fixed_threshold', large_grouped=False,
                          scoped_records=len(adapter.records), page_size=params['page_size'])
            cases.append(result)
        result = summarize(cases)
        self.assertEqual(result['single_response_cases'], 1)
        self.assertEqual(result['mid_chain_switch_cases'], 1)
        self.assertEqual(result['multiple_handle_response_cases'], 2)
        self.assertEqual(sum(row['first_response_spill_cases'] for row in result['coverage']), 1)


if __name__ == '__main__':
    unittest.main()
