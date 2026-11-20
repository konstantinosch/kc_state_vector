"""Accounting and end-to-end safeguards for the paired comparison harness."""
import unittest

from reference.model import Item, Plan, Stage
from reference.placement.adapters import Adapter
from reference.placement.protocol import Store, canonical
from reference.placement.two_way_checks import (
    clone_store, make_baseline, paired_case, storage_breakdown,
)
from reference.placement.two_way import Service
from .fixtures import PARAMS, ScriptedAdapter, first_handle


class ComparisonHarnessTests(unittest.TestCase):
    def test_storage_counts_replay_token_after_vector_copies_are_removed(self):
        service = Service(ScriptedAdapter(), threshold=4096)
        _, first = first_handle(service)
        before = storage_breakdown(service.store)
        self.assertGreater(before['hot_bytes'], 0)
        self.assertGreater(before['backup_bytes'], 0)
        second = service.request(first['continuation'], **PARAMS)
        after = storage_breakdown(service.store)
        self.assertEqual(after['vector_bytes'], 0)
        self.assertFalse(after['vector_resident'])
        self.assertTrue(after['coordination_resident'])
        self.assertGreater(after['coordination_bytes'], len(second['continuation']))
        encoded = canonical({'chain': second['chain'],
                             'record': service.store.heads[second['chain']]}).encode('ascii')
        self.assertEqual(after['total_bytes'], len(encoded))
        self.assertEqual(after['total_bytes'], after['coordination_bytes'])

    def test_failure_store_clone_is_independent_and_preserves_recovery_setting(self):
        service = Service(ScriptedAdapter(), threshold=4096, store=Store(recovery=False))
        _, first = first_handle(service)
        copy = clone_store(service.store)
        self.assertFalse(copy.recovery)
        self.assertEqual(copy.snapshot(), service.store.snapshot())
        copy.hot[first['chain']]['state']['padding'] = 'different'
        self.assertNotEqual(copy.snapshot(), service.store.snapshot())
        self.assertNotEqual(copy.hot[first['chain']]['state'],
                            service.store.hot[first['chain']]['state'])

    def test_paired_public_pager_trace_preserves_rows_and_accounts_saved_storage(self):
        records = tuple(Item(f'item-{index:03d}', index) for index in range(240, 0, -1))
        adapter = Adapter(Plan((Stage('priority', records[::2]), Stage('rest', records)),
                               scope='two-way-harness-regression'))
        params = {'page_size': 3, 'batch_size': 7, 'candidate_budget': 50}
        baseline = make_baseline(adapter, params)
        result = paired_case(adapter, params, baseline, threshold=2048)
        self.assertTrue(result['logical_results_identical'])
        self.assertTrue(result['all_checks_passed'])
        self.assertGreater(result['two_way']['summary']['requests'], 2)
        self.assertGreater(len(result['two_way']['summary']['return_inline_requests']), 0)
        self.assertGreater(result['saved']['vector_resident_pages'], 0)
        self.assertEqual(result['saved']['coordination_resident_pages'], 0)
        for mode in ('one_way', 'two_way'):
            pages = result[mode]['pages']
            self.assertEqual(result[mode]['summary']['rows'], len(adapter.expected_rows()))
            self.assertEqual(result[mode]['summary']['total_byte_pages'],
                             sum(page['total_bytes'] for page in pages))
            self.assertTrue(result[mode]['failures']['byte_identical'])


if __name__ == '__main__':
    unittest.main()
