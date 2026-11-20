"""Independent safeguards for actual retry-work and storage measurements."""
import unittest

from reference.model import Item, Plan, Stage
from reference.placement.adapters import Adapter
from reference.placement.recompute_checks import (
    CountingAdapter, paired_case, storage_breakdown,
)
from reference.placement.recompute import Service
from .fixtures import PARAMS, actual_core, scripted, returned


class ComparisonChecksTests(unittest.TestCase):
    def test_repeated_inputs_invoke_actual_selector_and_partition_failed_work(self):
        delegate = actual_core()
        counted = CountingAdapter(delegate)
        with counted.during('advance'):
            first = counted.select(None, PARAMS)
        with counted.during('retry'):
            second = counted.select(None, PARAMS)
        self.assertEqual(first, second)
        self.assertEqual(len(delegate.calls), 2)
        delegate.fail_next = True
        with self.assertRaises(RuntimeError):
            with counted.during('retry'):
                counted.select(None, PARAMS)
        self.assertEqual(counted.phase, 'unspecified')
        self.assertEqual(len(delegate.calls), 3)
        counts = counted.snapshot()
        self.assertEqual(counts['total']['attempts'], 3)
        self.assertEqual(counts['total']['select_calls'], 3)
        self.assertEqual(counts['total']['completed_calls'], 2)
        self.assertEqual(counts['total']['failed_calls'], 1)
        self.assertEqual(counts['total']['best_effort_calls'], 0)
        for name in ('consumed', 'fetched', 'reads'):
            self.assertEqual(counts['total'][name], 2 * first['work'][name])
            self.assertEqual(counts['phases']['advance'][name], first['work'][name])
            self.assertEqual(counts['phases']['retry'][name], second['work'][name])
        self.assertEqual(counts['phases']['retry']['attempts'], 2)
        self.assertEqual(counts['phases']['retry']['failed_calls'], 1)

    def test_storage_partitions_return_bridge_and_following_compact_metadata(self):
        service = Service(scripted(), threshold=4096)
        _, _, second = returned(service)
        bridge = storage_breakdown(service.store)
        self.assertEqual(bridge['vector_bytes'], 0)
        self.assertEqual(bridge['full_response_heads'], 1)
        self.assertEqual(bridge['compact_heads'], 0)
        self.assertGreater(bridge['response_payload_bytes'], len(second['continuation']))
        third = service.request(second['continuation'], **PARAMS)
        compact = storage_breakdown(service.store)
        self.assertEqual(service.inspect(third['continuation'])['kind'], 'inline')
        self.assertEqual(compact['vector_bytes'], 0)
        self.assertEqual(compact['full_response_heads'], 0)
        self.assertEqual(compact['compact_heads'], 1)
        self.assertEqual(compact['response_payload_bytes'], 0)
        self.assertGreater(compact['head_metadata_bytes'], 0)
        self.assertLess(compact['total_bytes'], bridge['total_bytes'])
        for value in (bridge, compact):
            self.assertEqual(value['heads_bytes'],
                             value['head_metadata_bytes'] + value['response_payload_bytes'])
            self.assertEqual(value['total_bytes'], sum(value[name + '_bytes'] for name in
                                                      ('hot', 'backup', 'heads', 'redirects')))
        self.assertEqual(compact['total_bytes'], service.store.storage_bytes())

    def test_real_pager_pair_keeps_healthy_costs_and_wire_equal_but_retries_cost_more(self):
        adapter = actual_core(size=240).delegate
        params = {'page_size': 3, 'batch_size': 7, 'candidate_budget': 50}
        result = paired_case(adapter, params, adapter.expected_rows(), threshold=2048)
        self.assertTrue(result['actual_selector_used'])
        self.assertTrue(result['logical_results_identical'])
        self.assertTrue(result['wire_bytes_identical'])
        old, new = result['full_response'], result['recompute']
        self.assertGreater(len(new['summary']['return_requests']), 0)
        self.assertEqual(old['summary']['selector'], new['summary']['selector'])
        self.assertEqual(old['summary']['token_bytes'], new['summary']['token_bytes'])
        self.assertGreater(result['extra_retry']['select_calls'], 0)
        self.assertGreater(result['extra_retry']['reads'], 0)
        self.assertGreater(result['saved']['response_payload_byte_pages'], 0)
        self.assertEqual(result['saved']['vector_byte_pages'], 0)
        self.assertEqual(result['saved']['token_bytes'], 0)
        for mode in (old, new):
            self.assertEqual(mode['summary']['rows'], len(adapter.expected_rows()))
            self.assertEqual(mode['summary']['selector']['total']['select_calls'],
                             mode['summary']['requests'])
            self.assertEqual(mode['failures']['counts']['fresh_worker_retries'],
                             mode['summary']['requests'])
            self.assertEqual(mode['summary']['total_byte_pages'],
                             sum(p['total_bytes'] for p in mode['pages']))
            for page in mode['failures']['retry_pages']:
                if page['input_kind'] == 'handle':
                    self.assertEqual(page['selector_calls'], 0)
        self.assertTrue(any(p['input_managed'] and p['input_kind'] == 'inline'
                            and p['selector_calls'] == 1 for p in new['failures']['retry_pages']))

    def test_single_response_unmanaged_control_has_same_retry_cost_and_no_state(self):
        adapter = Adapter(Plan((Stage('one', (Item('only', 1),)),), scope='recompute-single'))
        result = paired_case(adapter, {'page_size': 50}, adapter.expected_rows(), threshold=4096)
        self.assertEqual(result['extra_retry'], {'select_calls': 0, 'consumed': 0, 'fetched': 0, 'reads': 0})
        for name in ('full_response', 'recompute'):
            result_mode = result[name]
            self.assertTrue(result_mode['summary']['single_response'])
            self.assertEqual(result_mode['summary']['total_byte_pages'], 0)
            self.assertEqual(result_mode['summary']['token_bytes'], 0)
            self.assertEqual(result_mode['failures']['selector']['phases']['retry']['select_calls'], 1)


if __name__ == '__main__':
    unittest.main()
