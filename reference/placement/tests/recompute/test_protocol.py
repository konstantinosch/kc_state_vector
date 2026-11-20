"""Observable recompute contracts, including real source reads on retry."""
from copy import deepcopy
import unittest

from reference.model import Plan, Stage, ReadError
from reference.grouped.scenarios import targeted_scenarios
from reference.placement.adapters import Adapter
from reference.placement.protocol import (
    InjectedFailure, IntegrityError, InvalidToken, LostResponse, MissingState,
    PlacementError, RegistryUnavailable, RevisionConflict, StaleRevision, canonical,
)
from reference.placement.recompute import Service, Store
from .fixtures import (PARAMS, CountingAdapter, MutableCoreSource, actual_core,
                       scripted, first_handle, returned, worker)


class RecomputeProtocolTests(unittest.TestCase):
    def service(self, sizes=(4000, 0, 0, 4000, 0, None), **options):
        return Service(scripted(sizes), threshold=4096, **options)

    def replay(self, service, token, **params):
        try:
            return worker(service).request(token, **(params or PARAMS))
        except PlacementError as exc:
            self.fail(f'An accepted deterministic response must replay: {exc}')

    def assert_compact(self, service, response):
        head = service.store.heads[response['chain']]
        self.assertIsNone(head.get('response'))
        self.assertEqual(head['revision'], response['revision'])
        self.assertEqual(len(head['response_digest']), 64)
        self.assertNotIn(response['continuation'], [v for v in head.values() if isinstance(v, str)])

    def test_first_spill_retries_execute_actual_core_and_grouped_selectors(self):
        case = next(c for c in targeted_scenarios() if c.name == 'middle_hole_subtract')
        adapters = [actual_core()] + [CountingAdapter(Adapter(case.plan, mode))
                                      for mode in ('ids', 'spans', 'hybrid')]
        for adapter in adapters:
            with self.subTest(grouped=adapter.grouped, representation=adapter.representation):
                service = Service(adapter, threshold=512)
                token, response = first_handle(service)
                self.assert_compact(service, response)
                accepted = service.store.snapshot()
                count = len(adapter.calls)
                replayed = self.replay(service, token)
                self.assertEqual(replayed, response)
                self.assertEqual(len(adapter.calls), count + 1)
                self.assertEqual(adapter.calls[-1], adapter.calls[-2])
                self.assertEqual(service.store.snapshot(), accepted)
                self.assertGreater(adapter.calls[-1]['work']['reads'], 0)

    def test_managed_inline_retry_recomputes_and_drops_prior_bridge_after_commit(self):
        service = self.service()
        _, first, second = returned(service)
        self.assertEqual(service.store.heads[first['chain']]['response'], second)
        third = service.request(second['continuation'], **PARAMS)
        self.assert_compact(service, third)
        self.assertFalse(service.store.hot)
        self.assertFalse(service.store.backup)
        accepted = service.store.snapshot()
        count = len(service.adapter.calls)
        self.assertEqual(self.replay(service, second['continuation']), third)
        self.assertEqual(len(service.adapter.calls), count + 1)
        self.assertEqual(service.store.snapshot(), accepted)

    def test_actual_source_deletion_fails_explicitly_without_advancement(self):
        source = MutableCoreSource()
        adapter = actual_core(source=source)
        service = Service(adapter, threshold=512)
        token, first = first_handle(service)
        accepted = service.store.snapshot()
        binding = adapter.binding
        source.hidden.add(first['rows'][0][1])
        before = (len(adapter.calls), source.reads)
        with self.assertRaises(ReadError):
            worker(service).request(token, **PARAMS)
        self.assertEqual(adapter.binding, binding)
        self.assertEqual(len(adapter.calls), before[0] + 1)
        self.assertGreater(source.reads, before[1])
        self.assertNotIn('rows', adapter.calls[-1])
        self.assertEqual(service.store.snapshot(), accepted)

    def test_actual_pager_change_behind_stale_binding_fails_response_digest(self):
        adapter = actual_core()
        service = Service(adapter, threshold=512)
        token, first = first_handle(service)
        accepted = service.store.snapshot()
        original = adapter.delegate.plan
        # Deliberately simulate a backend changing membership while retaining
        # its old externally advertised version. The changed pager is real;
        # none of its selected rows or continuation is patched after selection.
        changed = Plan((Stage('priority', original.stages[0].items[1:]),
                        Stage('rest', original.stages[1].items)), scope=original.scope)
        old_binding = adapter.binding
        adapter.delegate = Adapter(changed)
        self.assertNotEqual(adapter.delegate.binding, old_binding)
        self.assertEqual(adapter.binding, old_binding)
        count = len(adapter.calls)
        with self.assertRaises(IntegrityError):
            worker(service).request(token, **PARAMS)
        self.assertEqual(len(adapter.calls), count + 1)
        self.assertGreater(adapter.calls[-1]['work']['reads'], 0)
        self.assertNotEqual(adapter.calls[-1]['rows'], first['rows'])
        self.assertEqual(service.store.snapshot(), accepted)

    def test_changed_request_limits_are_rejected_before_recomputation(self):
        for name in ('page_size', 'batch_size', 'candidate_budget'):
            with self.subTest(name=name):
                service = self.service()
                token, _ = first_handle(service)
                accepted = service.store.snapshot()
                count = len(service.adapter.calls)
                try:
                    worker(service).request(token, **{**PARAMS, name: PARAMS[name] + 1})
                except PlacementError as exc:
                    self.assertIsInstance(exc, RevisionConflict)
                else:
                    self.fail('Different request limits must conflict with accepted replay')
                self.assertEqual(len(service.adapter.calls), count)
                self.assertEqual(service.store.snapshot(), accepted)

    def test_same_rows_but_changed_work_or_continuation_fail_full_digest(self):
        for field in ('work', 'state'):
            with self.subTest(field=field):
                service = self.service()
                token, _ = first_handle(service)
                accepted = service.store.snapshot()
                def change(page):
                    if field == 'work':
                        page['work']['reads'] += 1
                    else:
                        page['state']['padding'] += 'changed'
                service.adapter.change = change
                count = len(service.adapter.calls)
                with self.assertRaises(IntegrityError):
                    worker(service).request(token, **PARAMS)
                self.assertEqual(len(service.adapter.calls), count + 1)
                self.assertEqual(service.store.snapshot(), accepted)

    def test_corrupt_compact_response_digest_is_not_ignored(self):
        service = self.service()
        token, first = first_handle(service)
        service.store.heads[first['chain']]['response_digest'] = '0' * 64
        accepted = service.store.snapshot()
        with self.assertRaises(IntegrityError):
            worker(service).request(token, **PARAMS)
        self.assertEqual(service.store.snapshot(), accepted)

    def test_missing_compact_metadata_is_rejected_before_selection(self):
        for field in ('response_digest', 'current_token_hash', 'request_key', 'binding'):
            with self.subTest(field=field):
                service = self.service()
                token, first = first_handle(service)
                del service.store.heads[first['chain']][field]
                before = service.store.snapshot()
                count = len(service.adapter.calls)
                with self.assertRaises(IntegrityError):
                    worker(service).request(token, **PARAMS)
                self.assertEqual(service.store.snapshot(), before)
                self.assertEqual(len(service.adapter.calls), count)

    def test_first_spill_retry_preserves_unmanaged_input_size_at_threshold_edge(self):
        adapter = scripted((1500, 0, None))
        probe = Service(adapter, threshold=1000000)
        response = probe.request(probe.start('boundary'), **PARAMS)
        false_payload = probe.inspect(response['continuation'])
        self.assertFalse(false_payload['managed'])
        size = len(response['continuation'])
        true_payload = {**false_payload, 'managed': True}
        self.assertLessEqual(len(probe._encode(true_payload)), size - 1)
        service = Service(adapter, threshold=size - 1)
        token, first = first_handle(service, 'boundary')
        count = len(adapter.calls)
        self.assertEqual(self.replay(service, token), first)
        self.assertEqual(len(adapter.calls), count + 1)

    def test_selector_failure_on_retry_keeps_accepted_progress(self):
        service = self.service()
        token, first = first_handle(service)
        accepted = service.store.snapshot()
        service.adapter.fail_next = True
        with self.assertRaisesRegex(RuntimeError, 'Injected selector failure'):
            worker(service).request(token, **PARAMS)
        self.assertEqual(service.store.snapshot(), accepted)
        self.assertEqual(self.replay(service, token), first)

    def test_handle_return_bridge_replays_without_another_selection(self):
        service = self.service()
        _, first = first_handle(service)
        proposal = service.prepare(first['continuation'], **PARAMS)
        with self.assertRaises(LostResponse):
            service.commit(proposal, fault='after_commit')
        accepted = service.store.snapshot()
        self.assertEqual(service.store.heads[first['chain']]['response'], proposal.response)
        self.assertFalse(service.store.hot)
        self.assertFalse(service.store.backup)
        count = len(service.adapter.calls)
        service.adapter.fail_next = True
        self.assertEqual(self.replay(service, first['continuation']), proposal.response)
        self.assertEqual(len(service.adapter.calls), count)
        self.assertTrue(service.adapter.fail_next)
        self.assertEqual(service.store.snapshot(), accepted)

    def test_failed_successor_selection_and_commit_preserve_return_bridge(self):
        service = self.service()
        _, first, second = returned(service)
        accepted = service.store.snapshot()
        service.adapter.fail_next = True
        with self.assertRaises(RuntimeError):
            worker(service).request(second['continuation'], **PARAMS)
        self.assertEqual(service.store.snapshot(), accepted)
        self.assertEqual(self.replay(service, first['continuation']), second)
        proposal = service.prepare(second['continuation'], **PARAMS)
        with self.assertRaises(InjectedFailure):
            service.commit(proposal, fault='before_commit')
        self.assertEqual(service.store.snapshot(), accepted)
        self.assertEqual(self.replay(service, first['continuation']), second)
        third = service.commit(proposal)
        self.assert_compact(service, third)
        with self.assertRaises(StaleRevision):
            worker(service).request(first['continuation'], **PARAMS)

    def test_handle_to_terminal_keeps_bridge_until_explicit_expiry(self):
        service = self.service((4000, None))
        _, first = first_handle(service)
        proposal = service.prepare(first['continuation'], **PARAMS)
        with self.assertRaises(LostResponse):
            service.commit(proposal, fault='after_commit')
        self.assertIsNone(proposal.response['continuation'])
        count = len(service.adapter.calls)
        self.assertEqual(self.replay(service, first['continuation']), proposal.response)
        self.assertEqual(len(service.adapter.calls), count)
        self.assertEqual(service.store.heads[first['chain']]['response'], proposal.response)
        service.store.expire_replay(first['chain'])
        with self.assertRaises(StaleRevision):
            worker(service).request(first['continuation'], **PARAMS)

    def test_corrupted_return_bridge_cannot_bypass_integrity_or_select_again(self):
        service = self.service()
        _, first, second = returned(service)
        service.store.heads[second['chain']]['response']['rows'][0][1] = 'changed-row'
        before = service.store.snapshot()
        count = len(service.adapter.calls)
        with self.assertRaises(IntegrityError):
            worker(service).request(first['continuation'], **PARAMS)
        self.assertEqual(service.store.snapshot(), before)
        self.assertEqual(len(service.adapter.calls), count)

    def test_managed_inline_to_terminal_retry_recomputes_and_is_bounded_by_expiry(self):
        service = self.service((4000, 0, None))
        _, _, second = returned(service)
        terminal = service.request(second['continuation'], **PARAMS)
        self.assertIsNone(terminal['continuation'])
        self.assert_compact(service, terminal)
        accepted = service.store.snapshot()
        count = len(service.adapter.calls)
        self.assertEqual(self.replay(service, second['continuation']), terminal)
        self.assertEqual(len(service.adapter.calls), count + 1)
        self.assertEqual(service.store.snapshot(), accepted)
        service.store.expire_replay(terminal['chain'])
        count = len(service.adapter.calls)
        with self.assertRaises(StaleRevision):
            worker(service).request(second['continuation'], **PARAMS)
        self.assertEqual(len(service.adapter.calls), count)

    def test_first_spill_return_and_respill_survive_before_and_after_commit_failures(self):
        service = self.service((4000, 0, 4000, 0, None))
        token = service.start('cycles')
        for kind in ('handle', 'inline', 'handle', 'inline'):
            proposal = service.prepare(token, **PARAMS)
            before = service.store.snapshot()
            with self.assertRaises(InjectedFailure):
                service.commit(proposal, fault='before_commit')
            self.assertEqual(service.store.snapshot(), before)
            with self.assertRaises(LostResponse):
                service.commit(proposal, fault='after_commit')
            accepted = service.store.snapshot()
            response = self.replay(service, token)
            self.assertEqual(response, proposal.response)
            self.assertEqual(service.store.snapshot(), accepted)
            token = response['continuation']
            self.assertEqual(service.inspect(token)['kind'], kind)

    def test_concurrent_identical_inline_proposals_publish_one_response(self):
        service = self.service()
        token = service.start('race')
        left = service.prepare(token, **PARAMS)
        right = worker(service).prepare(token, **PARAMS)
        response = service.commit(left)
        accepted = service.store.snapshot()
        self.assertEqual(worker(service).commit(right), response)
        self.assertEqual(service.store.snapshot(), accepted)
        self.assert_compact(service, response)

    def test_competing_request_parameters_cannot_overwrite_compact_progress(self):
        service = self.service()
        token = service.start('race')
        left = service.prepare(token, **PARAMS)
        right = worker(service).prepare(token, **{**PARAMS, 'page_size': 2})
        service.commit(left)
        accepted = service.store.snapshot()
        with self.assertRaises(RevisionConflict):
            worker(service).commit(right)
        self.assertEqual(service.store.snapshot(), accepted)

    def test_recompute_prepared_before_advancement_is_rejected_at_commit(self):
        service = self.service()
        original, first = first_handle(service)
        replay = worker(service).prepare(original, **PARAMS)
        second = service.request(first['continuation'], **PARAMS)
        accepted = service.store.snapshot()
        with self.assertRaises((StaleRevision, RevisionConflict)):
            worker(service).commit(replay)
        self.assertEqual(service.store.snapshot(), accepted)
        self.assertEqual(service.store.heads[first['chain']]['response'], second)

    def test_expiry_between_recompute_and_commit_rejects_prepared_response(self):
        service = self.service()
        original, first = first_handle(service)
        replay = worker(service).prepare(original, **PARAMS)
        service.store.expire_replay(first['chain'])
        accepted = service.store.snapshot()
        with self.assertRaises(StaleRevision):
            worker(service).commit(replay)
        self.assertEqual(service.store.snapshot(), accepted)

    def test_checkpoint_loss_between_recompute_and_commit_cannot_recreate_it(self):
        service = self.service()
        original, first = first_handle(service)
        replay = worker(service).prepare(original, **PARAMS)
        service.store.lose_state(first['chain'])
        accepted = service.store.snapshot()
        with self.assertRaises(MissingState):
            worker(service).commit(replay)
        self.assertEqual(service.store.snapshot(), accepted)

    def test_changed_concurrent_response_cannot_win_after_first_acceptance(self):
        service = self.service()
        token = service.start('race')
        left = service.prepare(token, **PARAMS)
        service.adapter.change = lambda page: page['work'].__setitem__('reads', 99)
        right = worker(service).prepare(token, **PARAMS)
        service.commit(left)
        accepted = service.store.snapshot()
        with self.assertRaises(IntegrityError):
            worker(service).commit(right)
        self.assertEqual(service.store.snapshot(), accepted)

    def test_compact_replay_does_not_recreate_missing_handle_checkpoint(self):
        for stage in ('first_spill', 'respill'):
            with self.subTest(stage=stage):
                service = self.service((4000, 0, 4000, 0, None))
                token, first = first_handle(service)
                response = first
                if stage == 'respill':
                    second = service.request(first['continuation'], **PARAMS)
                    token = second['continuation']
                    response = service.request(token, **PARAMS)
                service.store.lose_state(response['chain'])
                accepted = service.store.snapshot()
                with self.assertRaises(MissingState):
                    worker(service).request(token, **PARAMS)
                self.assertEqual(service.store.snapshot(), accepted)
                self.assertFalse(service.store.hot)
                self.assertFalse(service.store.backup)

    def test_hot_eviction_allows_exact_compact_retry_from_backup(self):
        service = self.service()
        token, first = first_handle(service)
        service.store.drop_hot(first['chain'])
        self.assertEqual(self.replay(service, token), first)
        self.assertIn(first['chain'], service.store.hot)
        self.assertEqual(service.store.hot[first['chain']], service.store.backup[first['chain']])

    def test_missing_registry_or_managed_head_never_recreates_chain(self):
        for missing in ('head', 'registry'):
            with self.subTest(missing=missing):
                service = self.service()
                _, _, second = returned(service)
                if missing == 'head':
                    del service.store.heads[second['chain']]
                else:
                    service.store.lose_registry()
                before = service.store.snapshot()
                with self.assertRaises(RegistryUnavailable):
                    worker(service).request(second['continuation'], **PARAMS)
                self.assertEqual(service.store.snapshot(), before)

    def test_signed_changed_inline_state_is_rejected_before_selection(self):
        service = self.service()
        _, _, second = returned(service)
        payload = service.inspect(second['continuation'])
        payload['state']['padding'] = 'forged'
        before = service.store.snapshot()
        count = len(service.adapter.calls)
        with self.assertRaises(IntegrityError):
            worker(service).request(service._encode(payload), **PARAMS)
        self.assertEqual(len(service.adapter.calls), count)
        self.assertEqual(service.store.snapshot(), before)

    def test_old_inputs_are_stale_after_replay_window_advances(self):
        service = self.service()
        initial, first, second = returned(service)
        third = service.request(second['continuation'], **PARAMS)
        for token in (initial, first['continuation']):
            with self.subTest(token_revision=service.inspect(token)['revision']):
                with self.assertRaises(StaleRevision):
                    worker(service).request(token, **PARAMS)
        self.assertEqual(self.replay(service, second['continuation']), third)

    def test_changed_threshold_policy_or_reader_is_rejected_before_selection(self):
        for changes in ({'threshold': 4095}, {'policy': 'restart'}, {'reader': 'another'}):
            with self.subTest(changes=changes):
                service = self.service()
                token, _ = first_handle(service)
                count = len(service.adapter.calls)
                before = service.store.snapshot()
                with self.assertRaises(InvalidToken):
                    worker(service, **changes).request(token, **PARAMS)
                self.assertEqual(len(service.adapter.calls), count)
                self.assertEqual(service.store.snapshot(), before)

    def test_hysteresis_and_repeated_cycles_preserve_every_row(self):
        service = self.service((4000, 2500, 2800, 2400, 0, 2500, 4000, 2600, 0, None))
        token = service.start('band')
        kinds, rows = [], []
        while token is not None:
            response = service.request(token, **PARAMS)
            rows.extend(response['rows'])
            token = response['continuation']
            kinds.append('terminal' if token is None else service.inspect(token)['kind'])
        self.assertEqual(kinds, ['handle', 'handle', 'handle', 'handle', 'inline',
                                 'inline', 'handle', 'handle', 'inline', 'terminal'])
        self.assertEqual([row[1] for row in rows], [f'item-{i}' for i in range(10)])

    def test_recompute_and_other_chain_progress_remain_isolated(self):
        service = self.service()
        a, left = first_handle(service, 'chain-left')
        _, right = first_handle(service, 'chain-rght')
        other = {name: deepcopy(table.get(right['chain'])) for name, table in service.store.snapshot().items()}
        self.assertEqual(self.replay(service, a), left)
        next_left = service.request(left['continuation'], **PARAMS)
        self.assertEqual(other, {name: table.get(right['chain']) for name, table in service.store.snapshot().items()})
        self.assertNotEqual(next_left['chain'], right['chain'])

    def test_recovery_fallbacks_keep_explicit_contract_and_stable_replay(self):
        for policy in ('best_effort', 'restart'):
            with self.subTest(policy=policy):
                service = self.service(policy=policy)
                _, first = first_handle(service)
                service.store.lose_state(first['chain'])
                proposal = service.prepare(first['continuation'], **PARAMS)
                with self.assertRaises(LostResponse):
                    service.commit(proposal, fault='after_commit')
                accepted = service.store.snapshot()
                response = self.replay(service, first['continuation'])
                self.assertEqual(response, proposal.response)
                self.assertEqual(response['status'], policy)
                self.assertEqual(response['client_action'], 'replace' if policy == 'restart' else 'append')
                self.assertNotEqual(response['chain'], first['chain'])
                self.assertEqual(service.store.snapshot(), accepted)
                rows = list(response['rows'])
                while response['continuation'] is not None:
                    response = service.request(response['continuation'], **PARAMS)
                    rows.extend(response['rows'])
                self.assertEqual([row[1] for row in rows],
                                 [f'item-{i}' for i in (range(6) if policy == 'restart' else range(1, 6))])


if __name__ == '__main__':
    unittest.main()
