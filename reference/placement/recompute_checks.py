"""Actual-selector comparison of full-response and recomputed inline replay.

No selection tape, memoized page, or cached transition is used. The independently
computed result oracle is comparison data only and is never served to a protocol.
"""
from collections import Counter, defaultdict
from contextlib import contextmanager
from copy import deepcopy

from reference.grouped.audit import audit_boundary as grouped_audit
from reference.placement.adapters import parameters
from reference.placement.protocol import (
    canonical, Store as FullStore, InjectedFailure, LostResponse, MissingState,
    StaleRevision,
)
from reference.placement.transition_checks import choose_points, traversal_bound
from reference.placement.two_way import Service as FullReplay
from .recompute import Service as RecomputeReplay, Store as RecomputeStore


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class CountingAdapter:
    """Transparent instrumentation; each call invokes the real supplied adapter."""
    def __init__(self, adapter):
        self.adapter = adapter
        self.phase = 'unspecified'
        self.counts = defaultdict(Counter)

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    @contextmanager
    def during(self, phase):
        old = self.phase
        self.phase = phase
        try:
            yield
        finally:
            self.phase = old

    def _call(self, name, value, params):
        count = self.counts[self.phase]
        count[name + '_calls'] += 1
        count['attempts'] += 1
        before = canonical(value)
        try:
            page = getattr(self.adapter, name)(value, params)
        except Exception:
            count['failed_calls'] += 1
            raise
        require(canonical(value) == before, 'selector mutated its input state')
        count['completed_calls'] += 1
        for key in ('consumed', 'fetched', 'reads'):
            count[key] += page['work'][key]
        return page

    def select(self, state, params):
        return self._call('select', state, params)

    def best_effort(self, cursor, params):
        return self._call('best_effort', cursor, params)

    def snapshot(self):
        names = ('attempts', 'select_calls', 'best_effort_calls', 'completed_calls',
                 'failed_calls', 'consumed', 'fetched', 'reads')
        phases = {phase: {name: count[name] for name in names}
                  for phase, count in sorted(self.counts.items())}
        total = {name: sum(count[name] for count in phases.values()) for name in names}
        return {'phases': phases, 'total': total}


def storage_breakdown(store):
    snapshot = store.snapshot()
    result = {name + '_bytes': sum(len(canonical({'chain': key, 'record': value}).encode('ascii'))
                                  for key, value in table.items())
              for name, table in snapshot.items()}
    full = [head['response'] for head in snapshot['heads'].values()
            if isinstance(head.get('response'), dict)]
    result['response_payload_bytes'] = sum(len(canonical(value).encode('ascii')) for value in full)
    # This partition includes all JSON syntax and record keys in metadata. It
    # subtracts only the complete nested response values, not an estimated size.
    result['head_metadata_bytes'] = result['heads_bytes'] - result['response_payload_bytes']
    result['vector_bytes'] = result['hot_bytes'] + result['backup_bytes']
    result['coordination_bytes'] = result['heads_bytes'] + result['redirects_bytes']
    result['total_bytes'] = result['vector_bytes'] + result['coordination_bytes']
    require(result['total_bytes'] == store.storage_bytes(), 'serialized store accounting differs')
    result['vector_resident'] = bool(snapshot['hot'] or snapshot['backup'])
    result['full_response_heads'] = len(full)
    result['compact_heads'] = sum(head.get('replay_mode') == 'recompute'
                                  for head in snapshot['heads'].values())
    return result


def clone_store(store):
    result = type(store)(recovery=store.recovery)
    for key, value in store.snapshot().items():
        setattr(result, key, value)
    result.available = store.available
    return result


def assert_result(service, response, expected, history):
    require(response['status'] == 'exact' and response['client_action'] == 'append',
            'exact traversal changed its result contract')
    history.extend(response['rows'])
    require(history == expected[:len(history)], 'actual selector disagrees with independent oracle prefix')
    state = None if response['continuation'] is None else service.logical_state(response['continuation'])
    if service.adapter.grouped:
        grouped_audit(service.adapter.plan, state, history,
                      expected=[(stage, identity, tuple(members)) for stage, identity, members in expected])
    else:
        service.adapter.audit(state, history)
    return state


def summarize(pages, counted):
    result = {'requests': len(pages), 'rows': sum(p['rows'] for p in pages),
              'single_response': len(pages) == 1,
              'inline_pages': sum(p['kind'] == 'inline' for p in pages),
              'handle_pages': sum(p['kind'] == 'handle' for p in pages),
              'vector_resident_pages': sum(p['vector_resident'] for p in pages),
              'full_response_head_pages': sum(p['full_response_heads'] for p in pages),
              'compact_head_pages': sum(p['compact_heads'] for p in pages),
              'token_bytes': sum(p['token_bytes'] for p in pages),
              'spill_requests': [p['request'] for p in pages if p['transition'] == 'inline_to_handle'],
              'return_requests': [p['request'] for p in pages if p['transition'] == 'handle_to_inline'],
              'selector': counted.snapshot()}
    result['respills'] = max(0, len(result['spill_requests']) - 1)
    for name in ('hot', 'backup', 'heads', 'redirects', 'vector', 'coordination',
                 'response_payload', 'head_metadata', 'total'):
        result[name + '_byte_pages'] = sum(p[name + '_bytes'] for p in pages)
        result['peak_' + name + '_bytes'] = max(p[name + '_bytes'] for p in pages)
    return result


def healthy(service_type, store_type, adapter, params, expected, threshold, backup):
    counted = CountingAdapter(adapter)
    service = service_type(counted, threshold=threshold, store=store_type(recovery=backup))
    token = service.start(chain='recompute-comparison')
    history, states, responses, pages = [], [], [], []
    for number in range(1, traversal_bound(adapter) + 1):
        incoming = service.inspect(token)
        with counted.during('advance'):
            response = service.request(token, **params)
        state = assert_result(service, response, expected, history)
        states.append(deepcopy(state))
        responses.append(deepcopy(response))
        token = response['continuation']
        kind = 'terminal' if token is None else service.inspect(token)['kind']
        token_bytes = 0 if token is None else len(token.encode('ascii'))
        require(token_bytes <= threshold, 'complete continuation exceeded T')
        pages.append({'request': number, 'rows': len(response['rows']), 'rows_so_far': len(history),
                      'input_kind': incoming['kind'], 'input_managed': incoming.get('managed', False), 'kind': kind,
                      'transition': incoming['kind'] + '_to_' + kind, 'token_bytes': token_bytes,
                      **storage_breakdown(service.store)})
        if token is None:
            break
    else:
        raise AssertionError('actual selection traversal exceeded its finite bound')
    require(history == expected, 'actual selector disagrees with complete independent oracle')
    require(counted.snapshot()['total']['select_calls'] == len(pages),
            'healthy selector did not execute exactly once per request')
    return {'states': states, 'responses': responses, 'pages': pages,
            'summary': summarize(pages, counted)}


def retry_and_faults(service_type, store_type, adapter, params, threshold, backup, control):
    """Retry every accepted request once; add explicitly partitioned fault work."""
    counted = CountingAdapter(adapter)
    service = service_type(counted, threshold=threshold, store=store_type(recovery=backup))
    token = service.start(chain='recompute-comparison')
    handles = [p['request'] for p in control['pages'] if p['input_kind'] == 'handle']
    switches = set(control['summary']['spill_requests'] + control['summary']['return_requests'])
    points = switches | set(choose_points(handles, 3))
    points |= {n + 1 for n in control['summary']['return_requests'] if n < len(control['pages'])}
    if control['pages'][-1]['input_kind'] == 'handle':
        points.add(len(control['pages']))
    counts = Counter()
    stale_after = {}
    retry_pages = []

    def fresh(store=None):
        return service_type(counted, threshold=threshold, store=service.store if store is None else store)

    for number, expected_response in enumerate(control['responses'], 1):
        incoming = service.inspect(token)
        if incoming['kind'] == 'handle':
            if number in points:
                lost = clone_store(service.store)
                lost.lose_state(incoming['chain'])
                before = counted.snapshot()['total']['attempts']
                with counted.during('missing_state_probe'):
                    try:
                        fresh(lost).request(token, **params)
                    except MissingState:
                        counts['missing_state_rejections'] += 1
                    else:
                        raise AssertionError('exact handle silently recreated missing accepted state')
                require(before == counted.snapshot()['total']['attempts'], 'missing-state rejection selected data')
            if backup:
                service.store.drop_hot(incoming['chain'])
                counts['hot_evictions'] += 1
        original = token
        if number in points:
            with counted.during('failed_publication_selection'):
                failed = service.prepare(token, **params)
            contender = fresh()
            with counted.during('concurrent_selection'):
                other = contender.prepare(token, **params)
            before = service.store.snapshot()
            try:
                service.commit(failed, fault='before_commit')
            except InjectedFailure:
                counts['before_commit_failures'] += 1
            else:
                raise AssertionError('pre-publication failure did not fail')
            require(service.store.snapshot() == before, 'failed publication changed accepted records')
            # A recreated worker computes again after the pre-publication
            # failure; it cannot reuse a proposal retained in a dead worker.
            service = fresh()
            with counted.during('advance'):
                accepted = service.prepare(token, **params)
            try:
                service.commit(accepted, fault='after_commit')
            except LostResponse:
                counts['lost_responses'] += 1
            else:
                raise AssertionError('lost response injection did not fail')
            snapshot = service.store.snapshot()
            with counted.during('concurrent_commit'):
                response = contender.commit(other)
            require(canonical(response) == canonical(expected_response), 'concurrent winner response changed')
            require(service.store.snapshot() == snapshot, 'concurrent commit advanced accepted progress twice')
            counts['concurrent_identical_commits'] += 1
        else:
            with counted.during('advance'):
                response = service.request(token, **params)
        require(canonical(response) == canonical(expected_response), 'fault path changed complete accepted response')
        state = None if response['continuation'] is None else service.logical_state(response['continuation'])
        require(state == control['states'][number - 1], 'fault path changed accepted logical vector')
        require(storage_breakdown(service.store) == {
            key: value for key, value in control['pages'][number - 1].items()
            if key.endswith('_bytes') and key != 'token_bytes'
            or key in ('vector_resident', 'full_response_heads', 'compact_heads')},
            'fault path storage differs from healthy boundary')
        before = service.store.snapshot()
        costs_before = counted.snapshot()['total']
        service = fresh()
        with counted.during('retry'):
            replay = service.request(original, **params)
        costs_after = counted.snapshot()['total']
        require(canonical(replay) == canonical(response), 'fresh-worker retry changed canonical response')
        require(service.store.snapshot() == before, 'fresh-worker retry changed accepted records')
        expected_retry_calls = int(incoming['kind'] == 'inline' and (
            service_type is RecomputeReplay or incoming['chain'] not in before['heads']))
        require(costs_after['select_calls'] - costs_before['select_calls'] == expected_retry_calls,
                'retry did not execute its declared actual-selector policy')
        counts['fresh_worker_retries'] += 1
        if number in control['summary']['return_requests']:
            require(not service.store.hot and not service.store.backup, 'return retained dedicated vector copies')
            counts['return_bridge_retries'] += 1
        retry_pages.append({'request': number, 'input_kind': incoming['kind'],
                            'input_managed': incoming.get('managed', False),
                            'selector_calls': costs_after['select_calls'] - costs_before['select_calls'],
                            'consumed': costs_after['consumed'] - costs_before['consumed'],
                            'fetched': costs_after['fetched'] - costs_before['fetched'],
                            'reads': costs_after['reads'] - costs_before['reads']})
        if number in stale_after:
            snapshot = service.store.snapshot()
            before = counted.snapshot()['total']['attempts']
            with counted.during('stale_probe'):
                try:
                    service.request(stale_after[number], **params)
                except StaleRevision:
                    counts['stale_rejections'] += 1
                else:
                    raise AssertionError('expired switch input was accepted after later progress')
            require(service.store.snapshot() == snapshot, 'stale rejection changed accepted records')
            require(counted.snapshot()['total']['attempts'] == before, 'stale input unnecessarily selected')
        if number in switches and number < len(control['pages']):
            stale_after[number + 1] = original
        token = response['continuation']
    require(token is None, 'fault traversal did not complete')
    return {'requests': len(control['pages']), 'fault_requests': sorted(points),
            'counts': dict(counts), 'selector': counted.snapshot(), 'retry_pages': retry_pages,
            'all_checks_passed': True}


def paired_case(adapter, params, expected, threshold, backup=True):
    params = parameters(params)
    controls = {}
    results = {}
    for mode, service_type, store_type in (
            ('full_response', FullReplay, FullStore),
            ('recompute', RecomputeReplay, RecomputeStore)):
        control = healthy(service_type, store_type, adapter, params, expected, threshold, backup)
        controls[mode] = control
        results[mode] = {'summary': control['summary'], 'pages': control['pages'],
                         'failures': retry_and_faults(service_type, store_type, adapter, params,
                                                     threshold, backup, control)}
    left, right = controls['full_response'], controls['recompute']
    require(len(left['responses']) == len(right['responses']), 'recompute changed request count')
    require(left['states'] == right['states'], 'recompute changed actual logical selection states')
    for a, b, pa, pb in zip(left['responses'], right['responses'], left['pages'], right['pages']):
        for field in ('rows', 'work', 'status', 'client_action', 'chain', 'revision'):
            require(a[field] == b[field], 'recompute changed result field: ' + field)
        require((pa['input_kind'], pa['kind'], pa['token_bytes']) ==
                (pb['input_kind'], pb['kind'], pb['token_bytes']), 'replay strategy changed wire size or placement')
    require(left['summary']['selector'] == right['summary']['selector'], 'replay strategy changed healthy selection cost')
    saved = {key: left['summary'][key] - right['summary'][key] for key in (
        'total_byte_pages', 'coordination_byte_pages', 'vector_byte_pages',
        'response_payload_byte_pages', 'head_metadata_byte_pages', 'token_bytes')}
    old_retry = results['full_response']['failures']['selector']['phases'].get('retry', {})
    new_retry = results['recompute']['failures']['selector']['phases'].get('retry', {})
    extra_retry = {key: new_retry.get(key, 0) - old_retry.get(key, 0)
                   for key in ('select_calls', 'consumed', 'fetched', 'reads')}
    return {'threshold_bytes': threshold, 'backup_enabled': backup, **results,
            'saved': saved, 'extra_retry': extra_retry,
            'actual_selector_used': True, 'logical_results_identical': True,
            'wire_bytes_identical': True, 'all_checks_passed': True}
