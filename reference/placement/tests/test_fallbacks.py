"""Witnesses for the explicitly different meanings of recovery, resume and restart."""
from copy import deepcopy
import unittest

from reference.model import Item, Plan, Reservation, Stage
from reference.grouped.model import Plan as GroupPlan, Record, Stage as GroupStage
from reference.grouped.scenarios import targeted_scenarios
from reference.placement.adapters import Adapter
from reference.placement.protocol import InjectedFailure, LostResponse, Service
from .test_protocol import first_spill, growth_adapter, priority_adapter, walk


def identities(rows):
    return [identity for _, _, members in rows for identity in members]


class FallbackTests(unittest.TestCase):
    def test_best_effort_repeats_earlier_priority_items(self):
        service, _, first = first_spill(policy='best_effort')
        token = first['continuation']
        old_chain = service.inspect(token)['chain']
        service.store.lose_state(old_chain)
        tail = walk(service, token, page_size=3)
        self.assertEqual(tail[0]['status'], 'best_effort')
        self.assertEqual(tail[0]['client_action'], 'append')
        self.assertNotEqual(tail[0]['chain'], old_chain)
        observed = identities(first['rows'] + [row for page in tail for row in page['rows']])
        self.assertEqual(observed, ['51', '49', '52', '51', '50', '49', '48'])
        self.assertNotEqual(observed, identities(service.adapter.expected_rows()))
        self.assertTrue(all(page['status'] == 'best_effort' for page in tail))

    def test_best_effort_can_omit_unvisited_items_above_last_position(self):
        a, z, y, b = Item('A', 10), Item('Z', 30), Item('Y', 20), Item('B', 5)
        adapter = Adapter(Plan((Stage('priority', (a,), quota=1), Stage('rest', (z, y, a, b))),
                               scope='omission'))
        service, _, first = first_spill(adapter, policy='best_effort', page_size=1)
        service.store.lose_state(service.inspect(first['continuation'])['chain'])
        pages = walk(service, first['continuation'], page_size=2)
        actual = identities(first['rows'] + [row for page in pages for row in page['rows']])
        self.assertEqual(actual, ['A', 'B'])
        self.assertEqual(set(identities(adapter.expected_rows())) - set(actual), {'Z', 'Y'})
        self.assertEqual(pages[0]['status'], 'best_effort')

    def test_best_effort_can_reintroduce_deliberately_omitted_reservations(self):
        a, b, reserved, x = Item('A', 30), Item('B', 20), Item('R', 10), Item('X', 5)
        adapter = Adapter(Plan((Stage('early', (a,), quota=1),
                                Stage('owner', (reserved,), quota=0),
                                Stage('rest', (b, reserved, x))),
                               (Reservation('owner', ('R',), on_unserved='omit'),), scope='omitted'))
        service, _, first = first_spill(adapter, policy='best_effort', page_size=1)
        self.assertIn('R', service.logical_state(first['continuation'])['omitted'])
        self.assertNotIn('R', identities(adapter.expected_rows()))
        service.store.lose_state(service.inspect(first['continuation'])['chain'])
        pages = walk(service, first['continuation'], page_size=2)
        self.assertIn('R', identities([row for page in pages for row in page['rows']]))
        self.assertTrue(all(page['status'] == 'best_effort' for page in pages))

    def test_best_effort_uses_full_tuple_with_equal_sort_keys(self):
        z, y, x, w = (Item(name, 10) for name in ('Z', 'Y', 'X', 'W'))
        adapter = Adapter(Plan((Stage('priority', (y,), quota=1), Stage('rest', (z, y, x, w))),
                               scope='ties'))
        service, _, first = first_spill(adapter, policy='best_effort', page_size=1)
        service.store.lose_state(service.inspect(first['continuation'])['chain'])
        pages = walk(service, first['continuation'], page_size=1)
        self.assertEqual([row[1] for page in pages for row in page['rows']], ['X', 'W'])
        self.assertEqual(pages[0]['status'], 'best_effort')

    def test_best_effort_can_repeat_a_previously_represented_group_member(self):
        records = (Record('A', 30, 'G'), Record('B', 20, 'H'), Record('C', 10, 'G'))
        plan = GroupPlan(records, (GroupStage('early', ('A',), quota=1),
                                   GroupStage('rest', ('A', 'B', 'C'))), scope='group-duplicate')
        for representation in ('ids', 'spans', 'hybrid'):
            with self.subTest(representation=representation):
                adapter = Adapter(plan, representation=representation)
                service, _, first = first_spill(adapter, policy='best_effort', page_size=1)
                self.assertEqual(first['rows'], [['early', 'A', ['A', 'C']]])
                service.store.lose_state(service.inspect(first['continuation'])['chain'])
                pages = walk(service, first['continuation'], page_size=1)
                self.assertEqual(identities([row for page in pages for row in page['rows']]), ['B', 'C'])
                self.assertEqual(identities(first['rows'] + [row for page in pages for row in page['rows']]).count('C'), 2)
                self.assertTrue(all(page['status'] == 'best_effort' for page in pages))

    def test_restart_explicitly_replaces_old_list_and_completes_new_oracle(self):
        service, _, first = first_spill(growth_adapter(), policy='restart', page_size=3)
        old_chain = service.inspect(first['continuation'])['chain']
        service.store.lose_state(old_chain)
        restarted = walk(service, first['continuation'], page_size=3)
        self.assertEqual(restarted[0]['status'], 'restart')
        self.assertEqual(restarted[0]['client_action'], 'replace')
        self.assertNotEqual(restarted[0]['chain'], old_chain)
        self.assertEqual([row for page in restarted for row in page['rows']], service.adapter.expected_rows())
        self.assertTrue(all(page['client_action'] == 'append' for page in restarted[1:]))
        self.assertTrue(all(page['status'] == 'exact' for page in restarted[1:]))

    def test_lost_fallback_response_replays_identical_new_chain(self):
        for policy in ('best_effort', 'restart'):
            with self.subTest(policy=policy):
                control, _, control_first = first_spill(growth_adapter(), policy=policy, page_size=3)
                service, _, first = first_spill(growth_adapter(), policy=policy, page_size=3)
                control.store.lose_state(control.inspect(control_first['continuation'])['chain'])
                service.store.lose_state(service.inspect(first['continuation'])['chain'])
                expected = control.request(control_first['continuation'], page_size=3)
                with self.assertRaises(LostResponse):
                    service.request(first['continuation'], page_size=3, fault='after_commit')
                accepted = deepcopy(service.store.heads)
                replay = service.request(first['continuation'], page_size=3)
                self.assertEqual(replay, expected)
                self.assertEqual(service.store.heads, accepted)
                self.assertEqual(service.request(first['continuation'], page_size=3), expected)

    def test_fallback_before_commit_failure_does_not_publish_a_replacement(self):
        for policy in ('best_effort', 'restart'):
            with self.subTest(policy=policy):
                service, _, first = first_spill(growth_adapter(), policy=policy, page_size=3)
                token = first['continuation']
                service.store.lose_state(service.inspect(token)['chain'])
                before = service.store.snapshot()
                with self.assertRaises(InjectedFailure):
                    service.request(token, page_size=3, fault='before_commit')
                self.assertEqual(service.store.snapshot(), before)
                recovered = service.request(token, page_size=3)
                self.assertEqual(recovered['status'], policy)

    def test_concurrent_fallbacks_publish_only_one_new_chain(self):
        for policy in ('best_effort', 'restart'):
            with self.subTest(policy=policy):
                service, _, first = first_spill(growth_adapter(), policy=policy, page_size=3)
                token = first['continuation']
                service.store.lose_state(service.inspect(token)['chain'])
                left = service.prepare(token, page_size=3)
                right = service.prepare(token, page_size=3)
                accepted = service.commit(left)
                before = service.store.snapshot()
                self.assertEqual(service.commit(right), accepted)
                self.assertEqual(service.store.snapshot(), before)

    def test_exact_recovery_preserves_partial_reservations_in_all_encodings(self):
        records = (Record('A', 30, 'G'), Record('B', 20, 'G'), Record('C', 10, 'G'),
                   Record('X', 5, 'H'))
        for policy in ('subtract', 'defer'):
            for completion in ('release', 'omit'):
                for representation in ('ids', 'spans', 'hybrid'):
                    with self.subTest(policy=policy, completion=completion, representation=representation):
                        plan = GroupPlan(records, (
                            GroupStage('early', ('A',), partial_policy=policy),
                            GroupStage('owner', ('B',), partial_policy=policy),
                            GroupStage('rest', ('A', 'B', 'C', 'X'), partial_policy=policy),
                        ), (Reservation('owner', ('B',), on_unserved=completion),), scope='reserved-hole')
                        adapter = Adapter(plan, representation=representation)
                        control, _, expected = first_spill(adapter, page_size=1, candidate_budget=1)
                        service, _, actual = first_spill(adapter, page_size=1, candidate_budget=1)
                        self.assertEqual(actual, expected)
                        rows = list(actual['rows'])
                        for _ in range(100):
                            token = actual['continuation']
                            if token is None:
                                break
                            service.store.drop_hot(service.inspect(token)['chain'])
                            actual = service.request(token, page_size=1, candidate_budget=1)
                            expected = control.request(expected['continuation'], page_size=1, candidate_budget=1)
                            self.assertEqual(actual, expected)
                            rows.extend(actual['rows'])
                        else:
                            self.fail('Reserved-group recovery did not terminate')
                        self.assertEqual(rows, adapter.expected_rows())

    def test_exact_recovery_preserves_unserved_and_deferred_owner_decisions(self):
        selected = {'defer_zero_release', 'defer_zero_omit',
                    'partial_owner_release', 'partial_owner_omit',
                    'overlapping_defer_release', 'overlapping_defer_omit'}
        cases = [case for case in targeted_scenarios() if case.name in selected]
        self.assertEqual({case.name for case in cases}, selected)
        for case in cases:
            for representation in ('ids', 'spans', 'hybrid'):
                with self.subTest(case=case.name, representation=representation):
                    adapter = Adapter(case.plan, representation=representation)
                    control, _, expected = first_spill(adapter, page_size=1, candidate_budget=1)
                    service, _, actual = first_spill(adapter, page_size=1, candidate_budget=1)
                    rows = list(actual['rows'])
                    self.assertEqual(actual, expected)
                    for _ in range(100):
                        if actual['continuation'] is None:
                            break
                        service.store.drop_hot(service.inspect(actual['continuation'])['chain'])
                        actual = service.request(actual['continuation'], page_size=1, candidate_budget=1)
                        expected = control.request(expected['continuation'], page_size=1, candidate_budget=1)
                        self.assertEqual(actual, expected)
                        rows.extend(actual['rows'])
                    else:
                        self.fail('Ownership recovery did not terminate')
                    self.assertEqual(rows, adapter.expected_rows())


if __name__ == '__main__':
    unittest.main()
