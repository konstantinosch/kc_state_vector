"""Independent observable checks for reversible placement and its replay ledger."""
from copy import deepcopy
import unittest

from reference.placement.protocol import (
    InjectedFailure, IntegrityError, InvalidToken, LostResponse,
    MissingState, PlacementError, RegistryUnavailable, RevisionConflict, StaleRevision, Store,
    Service as OneWayService,
)
from reference.placement.two_way import Service
from .fixtures import (PARAMS, ScriptedAdapter, first_handle, inline_size,
                       one_chain_snapshot, worker_like)


class TwoWayProtocolTests(unittest.TestCase):
    def service(self, sizes=(4000, 0, 3000, 0, None), **options):
        return Service(ScriptedAdapter(sizes), threshold=4096, **options)

    def assert_inline_storage(self, service, response):
        payload = service.inspect(response['continuation'])
        self.assertEqual(payload['kind'], 'inline')
        self.assertTrue(payload['managed'])
        self.assertNotIn(response['chain'], service.store.hot)
        self.assertNotIn(response['chain'], service.store.backup)
        self.assertIn(response['chain'], service.store.heads)
        self.assertEqual(service.store.heads[response['chain']]['response'], response)

    def test_spill_is_strictly_above_complete_token_threshold(self):
        adapter = ScriptedAdapter((1500, 0, None))
        size = inline_size(adapter, 0, False)
        for threshold, expected in ((size, 'inline'), (size - 1, 'handle')):
            with self.subTest(threshold=threshold):
                service = Service(adapter, threshold=threshold)
                response = service.request(service.start('chain-test'), **PARAMS)
                self.assertEqual(service.inspect(response['continuation'])['kind'], expected)
                self.assertLessEqual(len(response['continuation']), threshold)

    def test_return_is_strictly_below_half_including_odd_thresholds(self):
        adapter = ScriptedAdapter((5000, 300, 298, None))
        size = inline_size(adapter, 1, True)
        for threshold, expected in ((2 * size, 'handle'), (2 * size + 1, 'inline')):
            with self.subTest(threshold=threshold):
                service = Service(adapter, threshold=threshold)
                _, first = first_handle(service)
                second = service.request(first['continuation'], **PARAMS)
                self.assertEqual(service.inspect(second['continuation'])['kind'], expected)
                third = service.request(second['continuation'], **PARAMS)
                self.assertEqual(service.inspect(third['continuation'])['kind'], 'inline')

    def test_hysteresis_prevents_flapping_and_allows_repeated_real_switches(self):
        service = self.service((4000, 2500, 2800, 2400, 0, 2500, 4000, 2600, 0, None))
        token = service.start('chain-test')
        kinds, seen = [], []
        while token is not None:
            response = worker_like(service).request(token, **PARAMS)
            seen.extend(response['rows'])
            token = response['continuation']
            kinds.append('terminal' if token is None else service.inspect(token)['kind'])
        self.assertEqual(kinds, ['handle', 'handle', 'handle', 'handle', 'inline',
                                 'inline', 'handle', 'handle', 'inline', 'terminal'])
        self.assertEqual([row[1] for row in seen], [f'item-{i}' for i in range(10)])

    def test_return_removes_vector_copies_but_retains_replay_ledger(self):
        service = self.service()
        _, first = first_handle(service)
        before = service.store.storage_bytes()
        second = service.request(first['continuation'], **PARAMS)
        self.assert_inline_storage(service, second)
        self.assertGreater(service.store.storage_bytes(), 0)
        self.assertLess(service.store.storage_bytes(), before)
        self.assertEqual(service.logical_state(second['continuation']), {'next': 2, 'padding': ''})

    def test_lost_return_response_replays_old_handle_without_checkpoint(self):
        service = self.service()
        _, first = first_handle(service)
        proposal = service.prepare(first['continuation'], **PARAMS)
        with self.assertRaises(LostResponse):
            service.commit(proposal, fault='after_commit')
        self.assert_inline_storage(service, proposal.response)
        accepted = service.store.snapshot()
        for _ in range(3):
            try:
                replayed = worker_like(service).request(first['continuation'], **PARAMS)
            except PlacementError as exc:
                self.fail(f'Accepted inline response must replay without a vector checkpoint: {exc}')
            self.assertEqual(replayed, proposal.response)
            self.assertEqual(service.store.snapshot(), accepted)

    def test_failed_return_commit_preserves_old_vector_and_allows_retry(self):
        service = self.service()
        _, first = first_handle(service)
        proposal = service.prepare(first['continuation'], **PARAMS)
        before = service.store.snapshot()
        with self.assertRaises(InjectedFailure):
            service.commit(proposal, fault='before_commit')
        self.assertEqual(service.store.snapshot(), before)
        accepted = worker_like(service).request(first['continuation'], **PARAMS)
        self.assertEqual(accepted, proposal.response)
        self.assert_inline_storage(service, accepted)

    def test_first_spill_and_respill_lost_responses_replay_identically(self):
        service = self.service()
        token = service.start('chain-test')
        for expected in ('handle', 'inline', 'handle', 'inline'):
            proposal = service.prepare(token, **PARAMS)
            before = service.store.snapshot()
            with self.assertRaises(InjectedFailure):
                service.commit(proposal, fault='before_commit')
            self.assertEqual(service.store.snapshot(), before)
            with self.assertRaises(LostResponse):
                service.commit(proposal, fault='after_commit')
            accepted = service.store.snapshot()
            response = worker_like(service).request(token, **PARAMS)
            self.assertEqual(response, proposal.response)
            self.assertEqual(service.store.snapshot(), accepted)
            token = response['continuation']
            self.assertEqual(service.inspect(token)['kind'], expected)
        terminal = worker_like(service).request(token, **PARAMS)
        self.assertIsNone(terminal['continuation'])
        self.assertEqual(worker_like(service).request(token, **PARAMS), terminal)

    def test_same_input_concurrent_return_workers_publish_once(self):
        service = self.service()
        _, first = first_handle(service)
        left = service.prepare(first['continuation'], **PARAMS)
        right = worker_like(service).prepare(first['continuation'], **PARAMS)
        accepted = worker_like(service).commit(right)
        before = service.store.snapshot()
        self.assertEqual(service.commit(left), accepted)
        self.assertEqual(service.store.snapshot(), before)
        self.assert_inline_storage(service, accepted)

    def test_conflicting_concurrent_return_requests_do_not_overwrite(self):
        service = self.service()
        _, first = first_handle(service)
        left = service.prepare(first['continuation'], **PARAMS)
        other_params = {**PARAMS, 'page_size': 2}
        right = worker_like(service).prepare(first['continuation'], **other_params)
        service.commit(left)
        before = service.store.snapshot()
        with self.assertRaises(RevisionConflict):
            service.commit(right)
        self.assertEqual(service.store.snapshot(), before)
        with self.assertRaises(RevisionConflict):
            worker_like(service).request(first['continuation'], **other_params)

    def test_older_handle_is_stale_after_inline_progress(self):
        service = self.service((4000, 0, 0, 0, None))
        _, first = first_handle(service)
        second = service.request(first['continuation'], **PARAMS)
        third = service.request(second['continuation'], **PARAMS)
        self.assertEqual(service.inspect(third['continuation'])['kind'], 'inline')
        with self.assertRaises(StaleRevision):
            worker_like(service).request(first['continuation'], **PARAMS)
        accepted = service.store.snapshot()
        self.assertEqual(worker_like(service).request(second['continuation'], **PARAMS), third)
        self.assertEqual(service.store.snapshot(), accepted)

    def test_signed_but_changed_inline_state_or_metadata_is_rejected(self):
        for field in ('state', 'anchor', 'managed'):
            with self.subTest(field=field):
                service = self.service()
                _, first = first_handle(service)
                second = service.request(first['continuation'], **PARAMS)
                forged = deepcopy(service.inspect(second['continuation']))
                if field == 'state':
                    forged['state']['padding'] = 'different'
                elif field == 'anchor':
                    forged['anchor'] = service.adapter.position('item-0')
                else:
                    forged['managed'] = False
                before = service.store.snapshot()
                with self.assertRaises((IntegrityError, InvalidToken, RevisionConflict)):
                    worker_like(service).request(service._encode(forged), **PARAMS)
                self.assertEqual(service.store.snapshot(), before)

    def test_missing_individual_ledger_rejects_returned_inline_token(self):
        service = self.service()
        _, first = first_handle(service)
        second = service.request(first['continuation'], **PARAMS)
        del service.store.heads[second['chain']]
        self.assertFalse(service.store.hot)
        self.assertFalse(service.store.backup)
        before = service.store.snapshot()
        with self.assertRaises(RegistryUnavailable):
            worker_like(service).request(second['continuation'], **PARAMS)
        self.assertEqual(service.store.snapshot(), before)

    def test_missing_replay_rejects_old_handle_but_current_inline_can_progress(self):
        service = self.service()
        _, first = first_handle(service)
        second = service.request(first['continuation'], **PARAMS)
        service.store.forget_replay(second['chain'])
        with self.assertRaises(StaleRevision):
            worker_like(service).request(first['continuation'], **PARAMS)
        third = worker_like(service).request(second['continuation'], **PARAMS)
        self.assertEqual(third['revision'], second['revision'] + 1)

    def test_registry_outage_never_silently_restarts_returned_inline(self):
        for policy in ('exact', 'best_effort', 'restart'):
            with self.subTest(policy=policy):
                service = self.service(policy=policy)
                _, first = first_handle(service)
                second = service.request(first['continuation'], **PARAMS)
                service.store.lose_registry()
                with self.assertRaises(RegistryUnavailable):
                    worker_like(service).request(second['continuation'], **PARAMS)

    def test_hot_eviction_before_return_uses_latest_backup(self):
        service = self.service()
        _, first = first_handle(service)
        service.store.drop_hot(first['chain'])
        second = worker_like(service).request(first['continuation'], **PARAMS)
        self.assert_inline_storage(service, second)
        self.assertEqual(second['rows'][0][1], 'item-1')

    def test_loss_of_both_vector_copies_before_return_fails_exactly(self):
        service = self.service()
        _, first = first_handle(service)
        service.store.lose_state(first['chain'])
        before = service.store.snapshot()
        with self.assertRaises(MissingState):
            worker_like(service).request(first['continuation'], **PARAMS)
        self.assertEqual(service.store.snapshot(), before)

    def test_independent_return_and_respill_do_not_change_other_chain(self):
        service = self.service()
        _, left = first_handle(service, 'chain-left')
        _, right = first_handle(service, 'chain-rght')
        for _ in range(3):
            other = one_chain_snapshot(service.store, right['chain'])
            left = worker_like(service).request(left['continuation'], **PARAMS)
            self.assertEqual(one_chain_snapshot(service.store, right['chain']), other)
        self.assertEqual(service.inspect(left['continuation'])['kind'], 'inline')
        right = worker_like(service).request(right['continuation'], **PARAMS)
        self.assert_inline_storage(service, right)
        self.assertNotEqual(left['chain'], right['chain'])

    def test_binding_rejects_placement_threshold_reader_policy_or_plan_changes(self):
        service = self.service()
        _, first = first_handle(service)
        inline = service.request(first['continuation'], **PARAMS)['continuation']
        others = [
            Service(service.adapter, threshold=4095, store=service.store),
            Service(service.adapter, threshold=4096, policy='restart', store=service.store),
            Service(service.adapter, threshold=4096, reader='reader-2', store=service.store),
            Service(ScriptedAdapter((4001, 0, None)), threshold=4096, store=service.store),
            OneWayService(service.adapter, threshold=4096, store=service.store),
        ]
        before = service.store.snapshot()
        for index, other in enumerate(others):
            with self.subTest(index=index):
                with self.assertRaises(InvalidToken):
                    other.request(inline, **PARAMS)
                self.assertEqual(service.store.snapshot(), before)

    def test_unmanaged_small_chain_has_no_session_records(self):
        service = self.service((0, 0, None))
        token = service.start('small')
        while token is not None:
            response = worker_like(service).request(token, **PARAMS)
            token = response['continuation']
            if token is not None:
                self.assertFalse(service.inspect(token)['managed'])
            self.assertEqual(service.store.storage_bytes(), 0)

    def test_corrupted_saved_inline_replay_is_not_delivered(self):
        def wrong_rows(head):
            head['response']['rows'][0][1] = 'not-the-accepted-item'

        def wrong_revision(head):
            head['response']['revision'] += 1

        def wrong_response_digest(head):
            head['response_digest'] = '0' * 64

        def wrong_token_hash(head):
            head['current_token_hash'] = '0' * 64

        def wrong_state_digest(head):
            head['digest'] = '0' * 64

        for change in (wrong_rows, wrong_revision, wrong_response_digest,
                       wrong_token_hash, wrong_state_digest):
            with self.subTest(change=change.__name__):
                service = self.service()
                _, first = first_handle(service)
                second = service.request(first['continuation'], **PARAMS)
                change(service.store.heads[second['chain']])
                before = service.store.snapshot()
                with self.assertRaises(IntegrityError):
                    worker_like(service).request(first['continuation'], **PARAMS)
                self.assertEqual(service.store.snapshot(), before)

    def test_corrupted_checkpoint_before_return_never_triggers_fallback(self):
        for policy in ('exact', 'best_effort', 'restart'):
            for storage in ('hot', 'backup'):
                # Approximate policies intentionally do not restore the backup.
                if storage == 'backup' and policy != 'exact':
                    continue
                with self.subTest(policy=policy, storage=storage):
                    service = self.service(policy=policy)
                    _, first = first_handle(service)
                    if storage == 'backup':
                        service.store.drop_hot(first['chain'])
                    getattr(service.store, storage)[first['chain']]['state']['padding'] += 'corrupt'
                    before = service.store.snapshot()
                    with self.assertRaises(IntegrityError):
                        worker_like(service).request(first['continuation'], **PARAMS)
                    self.assertEqual(service.store.snapshot(), before)
                    self.assertFalse(service.store.redirects)

    def test_current_inline_competing_parameters_publish_only_one_advance(self):
        service = self.service((4000, 0, 0, 0, None))
        _, first = first_handle(service)
        second = service.request(first['continuation'], **PARAMS)
        left = service.prepare(second['continuation'], **PARAMS)
        right = worker_like(service).prepare(second['continuation'], **{**PARAMS, 'page_size': 2})
        accepted = worker_like(service).commit(right)
        before = service.store.snapshot()
        with self.assertRaises(RevisionConflict):
            service.commit(left)
        self.assertEqual(service.store.snapshot(), before)
        self.assert_inline_storage(service, accepted)

    def test_return_does_not_require_backup_storage(self):
        service = self.service(store=Store(recovery=False))
        _, first = first_handle(service)
        self.assertFalse(service.store.backup)
        second = service.request(first['continuation'], **PARAMS)
        self.assert_inline_storage(service, second)
        service.store.lose_state(second['chain'])
        self.assertEqual(worker_like(service).request(first['continuation'], **PARAMS), second)

    def test_fallback_after_respill_replays_and_preserves_new_chain_identity(self):
        for policy in ('best_effort', 'restart'):
            with self.subTest(policy=policy):
                service = self.service((4000, 0, 4000, 0, None), policy=policy)
                lost_tokens = []
                for chain in ('chain-left', 'chain-rght'):
                    _, first = first_handle(service, chain)
                    second = service.request(first['continuation'], **PARAMS)
                    third = service.request(second['continuation'], **PARAMS)
                    self.assertEqual(service.inspect(third['continuation'])['kind'], 'handle')
                    service.store.lose_state(chain)
                    lost_tokens.append(third['continuation'])

                left = worker_like(service).prepare(lost_tokens[0], **PARAMS)
                before = service.store.snapshot()
                with self.assertRaises(InjectedFailure):
                    service.commit(left, fault='before_commit')
                self.assertEqual(service.store.snapshot(), before)
                with self.assertRaises(LostResponse):
                    service.commit(left, fault='after_commit')
                right = worker_like(service).request(lost_tokens[1], **PARAMS)
                left_response = worker_like(service).request(lost_tokens[0], **PARAMS)
                self.assertEqual(left_response, left.response)
                self.assertNotEqual(left_response['chain'], right['chain'])
                self.assertNotIn(left_response['chain'], ('chain-left', 'chain-rght'))
                accepted = [left_response, right]
                for index, response in enumerate(accepted):
                    self.assertEqual(response['status'], policy)
                    self.assertEqual(response['client_action'], 'replace' if policy == 'restart' else 'append')
                    self.assertTrue(service.inspect(response['continuation'])['managed'])
                    seen = list(response['rows'])
                    kinds = []
                    other = one_chain_snapshot(service.store, accepted[1 - index]['chain'])
                    while response['continuation'] is not None:
                        kinds.append(service.inspect(response['continuation'])['kind'])
                        response = worker_like(service).request(response['continuation'], **PARAMS)
                        self.assertEqual(response['chain'], accepted[index]['chain'])
                        self.assertEqual(response['status'], 'exact' if policy == 'restart' else 'best_effort')
                        self.assertEqual(response['client_action'], 'append')
                        seen.extend(response['rows'])
                        self.assertEqual(one_chain_snapshot(service.store, accepted[1 - index]['chain']), other)
                    expected = range(5) if policy == 'restart' else range(3, 5)
                    self.assertEqual([row[1] for row in seen], [f'item-{i}' for i in expected])
                    self.assertEqual(kinds, ['handle', 'inline', 'handle', 'inline']
                                     if policy == 'restart' else ['inline'])
                    # Original fallback replays remain immutable after replacement completion.
                    self.assertEqual(worker_like(service).request(lost_tokens[index], **PARAMS), accepted[index])

    def test_recovered_inline_chain_still_requires_its_own_ledger(self):
        for policy in ('best_effort', 'restart'):
            with self.subTest(policy=policy):
                service = self.service(policy=policy)
                _, first = first_handle(service)
                service.store.lose_state(first['chain'])
                response = service.request(first['continuation'], **PARAMS)
                if policy == 'restart':
                    response = service.request(response['continuation'], **PARAMS)
                self.assert_inline_storage(service, response)
                del service.store.heads[response['chain']]
                with self.assertRaises(RegistryUnavailable):
                    worker_like(service).request(response['continuation'], **PARAMS)


if __name__ == '__main__':
    unittest.main()
