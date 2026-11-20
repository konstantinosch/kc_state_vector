"""Close the small grouped failure/capacity matrix without database measurements.

Existing hand-worked and generated tests establish output semantics. These tests
reuse selected fixtures to exercise every observed read and every returned-state
capacity boundary, including both partial-reservation policies and all encodings.
"""

from collections import Counter
import copy
import json
import unittest

from reference.model import Batch, CapacityError, MemorySource, ReadError
from reference.grouped.audit import audit_boundary
from reference.grouped.hybrid_scenarios import hybrid_scenarios
from reference.grouped.model import Pager
from reference.grouped.oracle import unpaged_oracle
from reference.grouped.scenarios import targeted_scenarios


MODES = ('ids', 'spans', 'hybrid')
SCHEDULES = (
    dict(page_size=1, batch_size=1, candidate_budget=1),
    dict(page_size=50, batch_size=1, candidate_budget=7),
)
CASE_NAMES = (
    'middle_hole_subtract', 'middle_hole_defer',
    'defer_zero_release', 'defer_zero_omit',
    'partial_owner_release', 'partial_owner_omit',
    'overlapping_defer_release', 'overlapping_defer_omit',
    'selective_yield_preemption',
    'hybrid_mixture_subtract', 'hybrid_mixture_defer',
    'hybrid_unicode_ties_subtract', 'hybrid_unicode_ties_defer',
)


def encoded_bytes(value):
    return len(json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=True, allow_nan=False).encode('utf-8'))


class OneReadFault(MemorySource):
    """Fail once at a counted source call, then behave as the complete source."""

    def __init__(self, read_number, kind):
        self.read_number, self.kind = read_number, kind
        self.calls = self.injected = 0

    def read(self, stage, cursor, limit):
        self.calls += 1
        healthy = super().read(stage, cursor, limit)
        if self.calls == self.read_number:
            self.injected += 1
            if self.kind == 'raised':
                raise OSError('deliberate grouped read failure')
            return Batch(healthy.items, healthy.exhausted, complete=False)
        return healthy


class PublicBoundaryTests(unittest.TestCase):
    def cases(self):
        available = {case.name: case for case in (*targeted_scenarios(), *hybrid_scenarios())}
        self.assertEqual(len(set(CASE_NAMES)), len(CASE_NAMES))
        self.assertTrue(set(CASE_NAMES) <= set(available))
        return tuple(available[name] for name in CASE_NAMES)

    def requests(self, plan, mode, arguments):
        expected = unpaged_oracle(plan)
        rows, state = [], None
        limit = sum(len(stage.candidate_ids) for stage in plan.stages) + len(plan.stages) + 1
        for request in range(1, limit + 1):
            incoming = json.loads(json.dumps(state))
            saved = copy.deepcopy(incoming)
            page = Pager(plan, mode).page(incoming, **arguments)
            self.assertEqual(incoming, saved)
            rows.extend(page.rows)
            self.assertEqual(rows, expected[:len(rows)])
            audit_boundary(plan, page.state, rows, expected=expected)
            yield request, incoming, page
            if page.state is None:
                self.assertEqual(rows, expected)
                return
            self.assertNotEqual(page.state, incoming)
            state = json.loads(json.dumps(page.state))
        self.fail('grouped boundary fixture did not terminate')

    def test_every_read_failure_preserves_input_and_exact_retry(self):
        counts = Counter()
        for case in self.cases():
            for mode in MODES:
                for arguments in SCHEDULES:
                    counts['traversals'] += 1
                    for request, incoming, expected in self.requests(case.plan, mode, arguments):
                        counts['boundaries'] += 1
                        counts['empty_nonterminal'] += not expected.rows and expected.state is not None
                        counts['multi_read_requests'] += expected.reads > 1
                        for read_number in range(1, expected.reads + 1):
                            for kind in ('raised', 'incomplete'):
                                with self.subTest(case=case.name, mode=mode, arguments=arguments,
                                                  request=request, read=read_number, kind=kind):
                                    supplied = copy.deepcopy(incoming)
                                    source = OneReadFault(read_number, kind)
                                    pager = Pager(case.plan, mode, source=source)
                                    with self.assertRaises(ReadError):
                                        pager.page(supplied, **arguments)
                                    self.assertEqual(source.injected, 1, 'fault injector was not reached')
                                    self.assertEqual(source.calls, read_number)
                                    self.assertEqual(supplied, incoming)
                                    recovered = pager.page(supplied, **arguments)
                                    fresh = Pager(case.plan, mode).page(copy.deepcopy(incoming), **arguments)
                                    self.assertEqual(supplied, incoming)
                                    self.assertEqual(source.injected, 1)
                                    self.assertEqual(recovered, expected)
                                    self.assertEqual(fresh, expected)
                                    counts[kind] += 1
                                    counts['late_read_faults'] += read_number > 1
        self.assertEqual(counts['traversals'], len(CASE_NAMES) * len(MODES) * len(SCHEDULES))
        self.assertGreater(counts['empty_nonterminal'], 0)
        self.assertGreater(counts['multi_read_requests'], 0)
        self.assertGreater(counts['late_read_faults'], 0)
        self.assertGreater(counts['raised'], 0)
        self.assertEqual(counts['raised'], counts['incomplete'])
        print('Grouped read-boundary matrix:', dict(sorted(counts.items())))

    def test_every_returned_capacity_boundary_and_terminal_retry(self):
        counts = Counter()
        for case in self.cases():
            for mode in MODES:
                for arguments in SCHEDULES:
                    counts['traversals'] += 1
                    for request, incoming, expected in self.requests(case.plan, mode, arguments):
                        with self.subTest(case=case.name, mode=mode, arguments=arguments, request=request):
                            counts['boundaries'] += 1
                            supplied = copy.deepcopy(incoming)
                            if expected.state is None:
                                # Capacity applies to emitted continuations, not the input
                                # or intermediate state. Terminal responses emit no vector.
                                terminal = Pager(case.plan, mode, max_state_entries=1,
                                                 max_state_bytes=1).page(supplied, **arguments)
                                self.assertEqual(terminal, expected)
                                self.assertEqual(supplied, incoming)
                                counts['terminal_minimum_limits'] += 1
                                continue
                            entries = sum(len(expected.state[key])
                                          for key in ('coverage', 'pending', 'omitted'))
                            byte_count = encoded_bytes(expected.state)
                            exact_limits = dict(max_state_entries=max(1, entries),
                                                max_state_bytes=byte_count)
                            exact = Pager(case.plan, mode, **exact_limits).page(supplied, **arguments)
                            self.assertEqual(exact, expected)
                            self.assertEqual(supplied, incoming)
                            counts['exact_fit'] += 1
                            counts['empty_nonterminal'] += not expected.rows
                            limits = [('bytes', dict(max_state_bytes=byte_count - 1))]
                            # The public grouped contract accepts positive limits only.
                            if entries > 1:
                                limits.append(('entries', dict(max_state_entries=entries - 1)))
                            for dimension, too_small in limits:
                                for _ in range(2):
                                    with self.assertRaises(CapacityError):
                                        Pager(case.plan, mode, **too_small).page(supplied, **arguments)
                                    self.assertEqual(supplied, incoming)
                                    counts['one_below_' + dimension] += 1
                                retry = Pager(case.plan, mode, **exact_limits).page(supplied, **arguments)
                                self.assertEqual(retry, expected)
                                self.assertEqual(supplied, incoming)
                                counts['raised_limit_retries'] += 1
        self.assertEqual(counts['traversals'], len(CASE_NAMES) * len(MODES) * len(SCHEDULES))
        self.assertEqual(counts['terminal_minimum_limits'], counts['traversals'])
        self.assertGreater(counts['exact_fit'], 0)
        self.assertGreater(counts['empty_nonterminal'], 0)
        self.assertGreater(counts['one_below_entries'], 0)
        self.assertEqual(counts['one_below_bytes'], counts['exact_fit'] * 2)
        self.assertEqual(counts['raised_limit_retries'] * 2,
                         counts['one_below_bytes'] + counts['one_below_entries'])
        print('Grouped capacity-boundary matrix:', dict(sorted(counts.items())))


if __name__ == '__main__':
    unittest.main()
