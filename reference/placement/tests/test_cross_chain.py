"""Independent lost chains and fresh workers must not alias recovery progress.

These assertions specify observable identity, replay and isolation properties.
They do not require a digest-based allocator: another globally unique allocator
with a reliable accepted-response mapping can satisfy the same contract.
"""
from copy import deepcopy
import unittest

from reference.placement.protocol import LostResponse, Service, Store
from .test_protocol import growth_adapter


PARAMS = {'page_size': 3, 'batch_size': 7, 'candidate_budget': 100}
POLICIES = ('best_effort', 'restart')


def lost_pair(policy):
    adapter, store = growth_adapter(), Store()

    def worker():
        return Service(adapter, threshold=512, policy=policy, store=store)

    tokens, anchors = [], []
    for chain in ('chain-left', 'chain-rght'):
        service = worker()
        first = service.request(service.start(chain), **PARAMS)
        token = first['continuation']
        envelope = service.inspect(token)
        if envelope['kind'] != 'handle' or envelope['revision'] != 1:
            raise AssertionError('Fixture requires two equally advanced spilled chains')
        tokens.append(token)
        anchors.append(envelope['anchor'])
    store.lose_state('chain-left')
    store.lose_state('chain-rght')
    return adapter, store, worker, tokens, anchors


def records_for(store, identities):
    """Snapshot one chain's original and replacement records in every table."""
    return {table: {key: deepcopy(value) for key, value in rows.items() if key in identities}
            for table, rows in store.snapshot().items()}


def expected_tail(adapter, policy, anchor):
    if policy == 'restart':
        return adapter.expected_rows()
    return [['best_effort', record.id, [record.id]] for record in adapter.records
            if record.position < tuple(anchor)]


class CrossChainRecoveryTests(unittest.TestCase):
    def assert_replacement_pair(self, responses, policy):
        identities = [response['chain'] for response in responses]
        self.assertEqual(len(set(identities)), 2, 'Different lost chains aliased one replacement')
        self.assertTrue(set(identities).isdisjoint({'chain-left', 'chain-rght'}))
        for response in responses:
            self.assertEqual(response['status'], policy)
            self.assertEqual(response['client_action'], 'replace' if policy == 'restart' else 'append')

    def test_distinct_lost_chains_recover_separately_across_fresh_workers(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                _, store, worker, tokens, _ = lost_pair(policy)
                left_worker, right_worker = worker(), worker()
                left = left_worker.prepare(tokens[0], **PARAMS)
                right = right_worker.prepare(tokens[1], **PARAMS)
                # Both proposals observed the loss before either was accepted.
                right_response = right_worker.commit(right)
                left_response = left_worker.commit(left)
                responses = [left_response, right_response]
                self.assert_replacement_pair(responses, policy)
                before = store.snapshot()
                # A recreated worker has no private allocator/replay memory.
                for index in (1, 0, 1, 0):
                    self.assertEqual(worker().request(tokens[index], **PARAMS), responses[index])
                    self.assertEqual(store.snapshot(), before)

    def test_recovery_progress_is_isolated_between_chains(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                adapter, store, worker, tokens, anchors = lost_pair(policy)
                responses = [worker().request(token, **PARAMS) for token in tokens]
                self.assert_replacement_pair(responses, policy)
                accepted = deepcopy(responses)
                seen = [list(response['rows']) for response in responses]
                identities = [
                    {'chain-left', responses[0]['chain']},
                    {'chain-rght', responses[1]['chain']},
                ]
                # Advance the left chain first, then interleave at unequal rates.
                for step in range(200):
                    index = 0 if step % 3 != 2 else 1
                    if responses[index]['continuation'] is None:
                        index = 1 - index
                    if responses[index]['continuation'] is None:
                        break
                    other = records_for(store, identities[1 - index])
                    responses[index] = worker().request(responses[index]['continuation'], **PARAMS)
                    seen[index].extend(responses[index]['rows'])
                    self.assertEqual(records_for(store, identities[1 - index]), other)
                    self.assertEqual(responses[index]['chain'], accepted[index]['chain'])
                    self.assertEqual(seen[index], expected_tail(adapter, policy, anchors[index])[:len(seen[index])])
                else:
                    self.fail('Independent finite recovery chains did not terminate')
                for index in range(2):
                    self.assertIsNone(responses[index]['continuation'])
                    self.assertEqual(seen[index], expected_tail(adapter, policy, anchors[index]))
                    # Advancing the replacement does not rewrite the accepted fallback.
                    self.assertEqual(worker().request(tokens[index], **PARAMS), accepted[index])

    def test_lost_fallback_response_replays_after_other_chain_recovers(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                _, store, worker, tokens, _ = lost_pair(policy)
                losing_worker = worker()
                proposal = losing_worker.prepare(tokens[0], **PARAMS)
                with self.assertRaises(LostResponse):
                    losing_worker.commit(proposal, fault='after_commit')
                right = worker().request(tokens[1], **PARAMS)
                untouched = records_for(store, {'chain-rght', right['chain']})
                left = worker().request(tokens[0], **PARAMS)
                self.assertEqual(left, proposal.response)
                self.assert_replacement_pair([left, right], policy)
                self.assertEqual(records_for(store, {'chain-rght', right['chain']}), untouched)
                self.assertEqual(worker().request(tokens[1], **PARAMS), right)

    def test_same_lost_chain_concurrent_workers_share_one_accepted_recovery(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                _, store, worker, tokens, _ = lost_pair(policy)
                left_worker, retry_worker = worker(), worker()
                proposal = left_worker.prepare(tokens[0], **PARAMS)
                competing = retry_worker.prepare(tokens[0], **PARAMS)
                accepted = retry_worker.commit(competing)
                right = worker().request(tokens[1], **PARAMS)
                before = store.snapshot()
                # No requirement that tentative allocators choose the same ID;
                # only the accepted result may become externally observable.
                self.assertEqual(left_worker.commit(proposal), accepted)
                self.assertEqual(store.snapshot(), before)
                self.assert_replacement_pair([accepted, right], policy)
                self.assertEqual(worker().request(tokens[0], **PARAMS), accepted)
                self.assertEqual(worker().request(tokens[1], **PARAMS), right)


if __name__ == '__main__':
    unittest.main()
