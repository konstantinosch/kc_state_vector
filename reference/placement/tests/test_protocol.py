"""Local placement protocol boundaries, independently specified by outcomes."""
from copy import deepcopy
import base64
import hashlib
import hmac
import json
import unittest

from reference.model import Item, MemorySource, Plan, ReadError, Reservation, Stage
from reference.placement.adapters import Adapter
from reference.placement.protocol import (
    Service, Store, InjectedFailure, IntegrityError, InvalidToken, LostResponse,
    MissingState, RegistryUnavailable, RevisionConflict, StaleRevision,
    TokenCapacityError,
)


def priority_adapter():
    records = {str(n): Item(str(n), n) for n in range(48, 53)}
    return Adapter(Plan((
        Stage('priority', tuple(records[n] for n in ('51', '49', '48')), quota=2),
        Stage('rest', tuple(records.values())),
    ), scope='placement-priority'))


def growth_adapter(size=60, source=None):
    items = tuple(Item(f'id{n:03d}', n) for n in range(size, 0, -1))
    return Adapter(Plan((Stage('early', items[::2]), Stage('rest', items)),
                        scope='placement-growth'), source=source)


def first_spill(adapter=None, *, policy='exact', store=None, page_size=3, **params):
    adapter = priority_adapter() if adapter is None else adapter
    arguments = {'page_size': page_size, **params}
    probe = Service(adapter, threshold=1_000_000, policy=policy)
    first = probe.request(probe.start(chain='fixture'), **arguments)
    if first['continuation'] is None:
        raise AssertionError('The fixture must leave a continuation')
    threshold = len(first['continuation'].encode('utf-8')) - 1
    service = Service(adapter, threshold=threshold, policy=policy, store=store)
    incoming = service.start(chain='fixture')
    response = service.request(incoming, **arguments)
    if service.inspect(response['continuation'])['kind'] != 'handle':
        raise AssertionError('The fixture did not exercise a first spill')
    return service, incoming, response


def walk(service, token, *, page_size=3):
    pages = []
    for _ in range(1000):
        page = service.request(token, page_size=page_size)
        pages.append(page)
        token = page['continuation']
        if token is None:
            return pages
    raise AssertionError('Fixture did not terminate')


class PlacementProtocolTests(unittest.TestCase):
    def test_threshold_counts_complete_token_and_is_inclusive(self):
        adapter = priority_adapter()
        probe = Service(adapter, threshold=1_000_000)
        start = probe.start(chain='boundary')
        expected = probe.request(start, page_size=3)
        exact_bytes = len(expected['continuation'].encode('utf-8'))
        at_limit = Service(adapter, threshold=exact_bytes)
        below_limit = Service(adapter, threshold=exact_bytes - 1)
        self.assertEqual(at_limit.start(chain='boundary'), start)
        self.assertEqual(below_limit.start(chain='boundary'), start)
        inline = at_limit.request(start, page_size=3)
        spilled = below_limit.request(start, page_size=3)
        self.assertEqual(inline, expected)
        self.assertEqual(at_limit.inspect(inline['continuation'])['kind'], 'inline')
        self.assertEqual(below_limit.inspect(spilled['continuation'])['kind'], 'handle')
        self.assertLessEqual(len(spilled['continuation'].encode()), exact_bytes - 1)
        self.assertEqual(inline['rows'], spilled['rows'])
        self.assertEqual(inline['work'], spilled['work'])
        self.assertEqual(at_limit.logical_state(inline['continuation']),
                         below_limit.logical_state(spilled['continuation']))

    def test_impossibly_small_budget_fails_explicitly(self):
        service = Service(priority_adapter(), threshold=1)
        with self.assertRaises(TokenCapacityError):
            service.start(chain='cannot-fit')
        self.assertEqual(service.store.heads, {})

    def test_handle_envelope_must_also_fit_the_budget(self):
        probe = Service(priority_adapter(), threshold=1_000_000)
        start = probe.start(chain='small-envelope')
        service = Service(priority_adapter(), threshold=len(start.encode()))
        before = service.store.snapshot()
        with self.assertRaises(TokenCapacityError):
            service.request(start, page_size=3)
        self.assertEqual(service.store.snapshot(), before)

    def test_spilled_chain_does_not_return_inline_as_coverage_shrinks(self):
        adapter = growth_adapter()
        probe = Service(adapter, threshold=1_000_000)
        expected = walk(probe, probe.start(chain='shrinking'), page_size=5)
        sizes = [len(page['continuation'].encode()) for page in expected
                 if page['continuation'] is not None]
        threshold = max(sizes) - 1
        self.assertGreater(max(sizes), min(sizes))
        service = Service(adapter, threshold=threshold)
        token = service.start(chain='shrinking')
        actual = []
        spilled = False
        later_small = False
        for reference in expected:
            observed = service.request(token, page_size=5)
            actual.append(observed)
            self.assertEqual(observed['rows'], reference['rows'])
            self.assertEqual(observed['work'], reference['work'])
            token = observed['continuation']
            if observed['continuation'] is None:
                self.assertIsNone(reference['continuation'])
                continue
            kind = service.inspect(observed['continuation'])['kind']
            if kind == 'handle':
                spilled = True
            if spilled:
                self.assertEqual(kind, 'handle')
                later_small |= len(reference['continuation'].encode()) <= threshold
        self.assertEqual(len(expected), len(actual))
        self.assertIsNone(token)
        self.assertTrue(spilled)
        self.assertTrue(later_small, 'Fixture must reach small state after spilling')
        self.assertEqual([row for page in actual for row in page['rows']], adapter.expected_rows())

    def test_lost_first_spill_response_replays_same_handle_and_page(self):
        control, incoming, expected = first_spill()
        service = Service(priority_adapter(), threshold=control.threshold)
        with self.assertRaises(LostResponse):
            service.request(incoming, page_size=3, fault='after_commit')
        recovered = service.request(incoming, page_size=3)
        self.assertEqual(recovered, expected)
        self.assertEqual(len(service.store.heads), 1)
        self.assertEqual(service.request(recovered['continuation'], page_size=3),
                         control.request(expected['continuation'], page_size=3))

    def test_matching_concurrent_first_spills_accept_one_transition(self):
        control, token, expected = first_spill()
        service = Service(priority_adapter(), threshold=control.threshold)
        left = service.prepare(token, page_size=3)
        right = service.prepare(token, page_size=3)
        self.assertEqual(service.commit(left), expected)
        self.assertEqual(service.commit(right), expected)
        self.assertEqual(len(service.store.heads), 1)

    def test_concurrent_stored_revisions_replay_or_conflict(self):
        for competing_size in (3, 4):
            with self.subTest(competing_size=competing_size):
                service, _, first = first_spill(growth_adapter(), page_size=3)
                token = first['continuation']
                left = service.prepare(token, page_size=3)
                right = service.prepare(token, page_size=competing_size)
                accepted = service.commit(left)
                heads = deepcopy(service.store.heads)
                if competing_size == 3:
                    self.assertEqual(service.commit(right), accepted)
                else:
                    with self.assertRaises(RevisionConflict):
                        service.commit(right)
                self.assertEqual(service.store.heads, heads)

    def test_conflicting_concurrent_requests_do_not_advance_twice(self):
        control, token, expected = first_spill(growth_adapter(), page_size=3)
        service = Service(growth_adapter(), threshold=control.threshold)
        winner = service.prepare(token, page_size=3)
        loser = service.prepare(token, page_size=4)
        self.assertEqual(service.commit(winner), expected)
        accepted = deepcopy(service.store.heads)
        with self.assertRaises(RevisionConflict):
            service.commit(loser)
        self.assertEqual(service.store.heads, accepted)

    def test_before_commit_failure_preserves_accepted_progress(self):
        control, token, expected = first_spill()
        service = Service(priority_adapter(), threshold=control.threshold)
        before = deepcopy(service.store.heads)
        with self.assertRaises(InjectedFailure):
            service.request(token, page_size=3, fault='before_commit')
        self.assertEqual(service.store.heads, before)
        self.assertEqual(service.store.hot, {})
        self.assertEqual(service.store.backup, {})
        self.assertEqual(service.request(token, page_size=3), expected)

    def test_last_transition_replays_but_older_revisions_are_rejected(self):
        service, initial, first = first_spill(growth_adapter(), page_size=3)
        second = service.request(first['continuation'], page_size=3)
        self.assertEqual(service.request(first['continuation'], page_size=3), second)
        with self.assertRaises(RevisionConflict):
            service.request(first['continuation'], page_size=4)
        with self.assertRaises(StaleRevision):
            service.request(initial, page_size=3)
        service.request(second['continuation'], page_size=3)
        with self.assertRaises(StaleRevision):
            service.request(first['continuation'], page_size=3)

    def test_expired_replay_does_not_continue_from_latest(self):
        service, incoming, first = first_spill(growth_adapter(), page_size=3)
        chain = service.inspect(first['continuation'])['chain']
        service.store.forget_replay(chain)
        before = deepcopy(service.store.heads)
        with self.assertRaises(StaleRevision):
            service.request(incoming, page_size=3)
        self.assertEqual(service.store.heads, before)

    def test_terminal_response_is_replayable(self):
        service, _, first = first_spill()
        token = first['continuation']
        terminal = service.request(token, page_size=50)
        self.assertIsNone(terminal['continuation'])
        self.assertEqual(service.request(token, page_size=50), terminal)
        self.assertEqual(terminal['status'], 'exact')

    def test_modifying_a_returned_response_cannot_poison_replay(self):
        service, incoming, first = first_spill()
        expected = deepcopy(first)
        first['rows'][0][1] = 'not-a-real-item'
        first['work']['consumed'] = 999
        self.assertEqual(service.request(incoming, page_size=3), expected)

    def test_hot_loss_recovers_the_latest_checkpoint(self):
        control, _, expected_first = first_spill(growth_adapter(), page_size=3)
        service, _, first = first_spill(growth_adapter(), page_size=3)
        self.assertEqual(first, expected_first)
        actual_token, expected_token = first['continuation'], expected_first['continuation']
        rows = list(first['rows'])
        for _ in range(100):
            chain = service.inspect(actual_token)['chain']
            service.store.drop_hot(chain)
            page = service.request(actual_token, page_size=3)
            expected = control.request(expected_token, page_size=3)
            self.assertEqual(page, expected)
            rows.extend(page['rows'])
            actual_token, expected_token = page['continuation'], expected['continuation']
            if actual_token is None:
                break
        else:
            self.fail('Recovered traversal failed to finish')
        self.assertEqual(rows, service.adapter.expected_rows())

    def test_missing_recovery_copy_is_not_a_restart(self):
        service, _, first = first_spill()
        chain = service.inspect(first['continuation'])['chain']
        service.store.lose_state(chain)
        before = deepcopy(service.store.heads)
        with self.assertRaises(MissingState):
            service.request(first['continuation'], page_size=3)
        self.assertEqual(service.store.heads, before)
        self.assertEqual(service.store.redirects, {})

    def test_corrupt_hot_vector_never_triggers_an_approximate_fallback(self):
        for policy in ('exact', 'best_effort', 'restart'):
            with self.subTest(policy=policy):
                service, _, first = first_spill(growth_adapter(), policy=policy, page_size=3)
                chain = service.inspect(first['continuation'])['chain']
                service.store.hot[chain]['state']['coverage'] = []
                accepted = deepcopy(service.store.heads)
                with self.assertRaises(IntegrityError):
                    service.request(first['continuation'], page_size=3)
                self.assertEqual(service.store.heads, accepted)
                self.assertEqual(service.store.redirects, {})

    def test_corrupt_backup_cannot_be_used_as_exact_recovery(self):
        service, _, first = first_spill(growth_adapter(), page_size=3)
        chain = service.inspect(first['continuation'])['chain']
        service.store.drop_hot(chain)
        service.store.backup[chain]['state']['coverage'] = []
        with self.assertRaises(IntegrityError):
            service.request(first['continuation'], page_size=3)
        self.assertNotIn(chain, service.store.hot)

    def test_malformed_checkpoints_have_typed_integrity_failures(self):
        for target in ('hot', 'backup'):
            for corruption in ('non-dict', 'unserializable', 'nonfinite', 'missing-field'):
                with self.subTest(target=target, corruption=corruption):
                    service, _, first = first_spill(growth_adapter(), page_size=3)
                    chain = service.inspect(first['continuation'])['chain']
                    table = getattr(service.store, target)
                    if target == 'backup':
                        service.store.drop_hot(chain)
                    if corruption == 'non-dict':
                        table[chain] = []
                    elif corruption == 'unserializable':
                        table[chain]['state'] = {'bad': {1, 2}}
                    elif corruption == 'nonfinite':
                        table[chain]['state'] = {'bad': float('nan')}
                    else:
                        del table[chain]['state']
                    accepted = deepcopy(service.store.heads)
                    with self.assertRaises(IntegrityError):
                        service.request(first['continuation'], page_size=3)
                    self.assertEqual(service.store.heads, accepted)
                    self.assertEqual(service.store.redirects, {})

    def test_older_backup_cannot_resume_the_latest_revision(self):
        service, _, first = first_spill(growth_adapter(), page_size=3)
        chain = service.inspect(first['continuation'])['chain']
        older = deepcopy(service.store.backup[chain])
        second = service.request(first['continuation'], page_size=3)
        service.store.drop_hot(chain)
        service.store.backup[chain] = older
        accepted = deepcopy(service.store.heads)
        with self.assertRaises(IntegrityError):
            service.request(second['continuation'], page_size=3)
        self.assertEqual(service.store.heads, accepted)
        self.assertNotIn(chain, service.store.hot)

    def test_authenticated_but_wrong_handle_digest_is_rejected(self):
        service, _, first = first_spill(growth_adapter(), page_size=3)
        payload = service.inspect(first['continuation'])
        payload['digest'] = '0' * 64
        # Independently construct an authenticated envelope to isolate the state
        # digest check from the signature check; this is a deliberately bad issuer.
        serialized = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                ensure_ascii=True, allow_nan=False).encode('ascii')
        body = base64.urlsafe_b64encode(serialized).rstrip(b'=').decode('ascii')
        signature = hmac.new(service.secret, body.encode('ascii'), hashlib.sha256).hexdigest()
        token = body + '.' + signature
        before = deepcopy(service.store.heads)
        with self.assertRaises(IntegrityError):
            service.request(token, page_size=3)
        self.assertEqual(service.store.heads, before)

    def test_authentication_and_reader_binding_precede_recovery(self):
        service, _, first = first_spill(policy='restart')
        token = first['continuation']
        chain = service.inspect(token)['chain']
        service.store.lose_state(chain)
        wrong_reader = Service(service.adapter, threshold=service.threshold,
                               policy='restart', reader='another-reader', store=service.store)
        with self.assertRaises(InvalidToken):
            wrong_reader.request(token, page_size=3)
        changed = token[:-1] + ('0' if token[-1] != '0' else '1')
        with self.assertRaises(InvalidToken):
            service.request(changed, page_size=3)
        self.assertEqual(service.store.redirects, {})

    def test_lost_coordination_history_never_silently_recreates_chain(self):
        for policy in ('exact', 'best_effort', 'restart'):
            with self.subTest(policy=policy):
                service, inline, first = first_spill(policy=policy)
                service.store.lose_registry()
                for token in (inline, first['continuation']):
                    with self.assertRaises(RegistryUnavailable):
                        service.request(token, page_size=3)

    def test_missing_head_with_surviving_checkpoint_cannot_recreate_from_old_inline(self):
        service, original_inline, first = first_spill(growth_adapter(), page_size=3)
        second = service.request(first['continuation'], page_size=3)
        chain = service.inspect(second['continuation'])['chain']
        del service.store.heads[chain]
        before = service.store.snapshot()
        with self.assertRaises(RegistryUnavailable):
            service.request(original_inline, page_size=3)
        self.assertEqual(service.store.snapshot(), before)

    def test_empty_page_preserves_last_delivered_anchor(self):
        a, b = Item('A', 30), Item('B', 20)
        adapter = Adapter(Plan((Stage('early', (a, b)), Stage('owner', (b,))),
                               (Reservation('owner', ('B',)),), scope='anchor'))
        service, _, first = first_spill(adapter, page_size=1, candidate_budget=1)
        self.assertEqual(first['rows'], [['early', 'A', ['A']]])
        first_payload = service.inspect(first['continuation'])
        empty = service.request(first['continuation'], page_size=1, candidate_budget=1)
        self.assertEqual(empty['rows'], [])
        self.assertIsNotNone(empty['continuation'])
        self.assertEqual(service.inspect(empty['continuation'])['anchor'], first_payload['anchor'])

    def test_source_failure_does_not_advance_stored_revision(self):
        class FailingSource(MemorySource):
            fail = False
            def read(self, stage, cursor, limit):
                if self.fail:
                    self.fail = False
                    raise RuntimeError('injected read failure')
                return super().read(stage, cursor, limit)
        source = FailingSource()
        service, _, first = first_spill(growth_adapter(source=source), page_size=3)
        control, _, control_first = first_spill(growth_adapter(), page_size=3)
        source.fail = True
        before = deepcopy(service.store.heads)
        with self.assertRaises(ReadError):
            service.request(first['continuation'], page_size=3)
        self.assertEqual(service.store.heads, before)
        self.assertEqual(service.request(first['continuation'], page_size=3),
                         control.request(control_first['continuation'], page_size=3))


if __name__ == '__main__':
    unittest.main()
