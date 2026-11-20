"""Paired placement checks over independently validated finite selection traces.

Every placement request executes the real protocol. Only pure selection transitions
are reused, keyed by complete logical input and request parameters. No latency or
real storage durability inference is made from this harness.
"""
from collections import Counter
from copy import deepcopy

from reference.placement.protocol import (
    Service as OneWay, Store, canonical, InjectedFailure, LostResponse,
    MissingState, PlacementError,
)
from reference.placement.transition_checks import (
    prepare_baseline, ValidatedSelection, choose_points, require,
)
from .two_way import Service as TwoWay


def make_baseline(adapter, params):
    result = prepare_baseline(adapter, params)
    result['validation'] = 'independent oracle plus every-boundary retirement audit'
    return result


def storage_breakdown(store):
    """Canonical bytes of each complete serialized record, including its key."""
    snapshot = store.snapshot()
    counts = {name + '_bytes': sum(len(canonical({'chain': key, 'record': value}).encode('ascii'))
                                  for key, value in table.items())
              for name, table in snapshot.items()}
    counts['vector_bytes'] = counts['hot_bytes'] + counts['backup_bytes']
    counts['coordination_bytes'] = counts['heads_bytes'] + counts['redirects_bytes']
    counts['total_bytes'] = counts['vector_bytes'] + counts['coordination_bytes']
    require(counts['total_bytes'] == store.storage_bytes(), 'byte accounting differs from Store')
    counts['vector_resident'] = bool(snapshot['hot'] or snapshot['backup'])
    counts['coordination_resident'] = bool(snapshot['heads'] or snapshot['redirects'])
    return counts


def clone_store(store):
    clone = Store(recovery=store.recovery)
    for key, value in store.snapshot().items():
        setattr(clone, key, value)
    clone.available = store.available
    return clone


def assert_page(service, response, expected):
    require(response['rows'] == expected['rows'], 'placement changed rows')
    require(response['work'] == expected['work'], 'placement changed selection work')
    state = None if response['continuation'] is None else service.logical_state(response['continuation'])
    require(state == expected['state'], 'placement changed logical state')
    require(response['status'] == 'exact' and response['client_action'] == 'append',
            'healthy exact placement changed its contract')


def healthy(service_type, adapter, params, baseline, threshold, recovery=True):
    tape = ValidatedSelection(adapter, baseline)
    service = service_type(tape, threshold=threshold, store=Store(recovery=recovery))
    token = service.start(chain='two-way-comparison')
    initial_bytes = len(token.encode('ascii'))
    inputs, responses, pages = [], [], []
    selected = 0
    for number, expected in enumerate(baseline['pages'], 1):
        incoming = service.inspect(token)
        inputs.append(token)
        response = service.request(token, **params)
        assert_page(service, response, expected)
        responses.append(response)
        token = response['continuation']
        kind = 'terminal' if token is None else service.inspect(token)['kind']
        size = 0 if token is None else len(token.encode('ascii'))
        require(size <= threshold, 'outgoing complete token exceeded T')
        selected += len(response['rows'])
        outgoing = {} if token is None else service.inspect(token)
        # Use each implementation's real envelope. Differences are disclosed;
        # thresholds apply to full authenticated tokens, not estimated vector JSON.
        potential = 0
        if token is not None:
            inline = {key: value for key, value in outgoing.items() if key != 'digest'}
            inline.update(kind='inline', state=expected['state'])
            potential = len(service._encode(inline).encode('ascii'))
        pages.append({'request': number, 'rows': len(response['rows']), 'rows_so_far': selected,
                      'input_kind': incoming['kind'], 'kind': kind, 'token_bytes': size,
                      'potential_inline_bytes': potential,
                      'transition': incoming['kind'] + '_to_' + kind,
                      **storage_breakdown(service.store)})
    require(token is None, 'placement did not complete')
    counts = Counter(page['kind'] for page in pages)
    spills = [p['request'] for p in pages if p['input_kind'] == 'inline' and p['kind'] == 'handle']
    returns = [p['request'] for p in pages if p['input_kind'] == 'handle' and p['kind'] == 'inline']
    metrics = {'requests': len(pages), 'rows': selected, 'single_response': len(pages) == 1,
               'inline_pages': counts['inline'], 'handle_pages': counts['handle'],
               'terminal_pages': counts['terminal'], 'spill_requests': spills,
               'return_inline_requests': returns, 'respills': max(0, len(spills) - 1),
               'vector_resident_pages': sum(p['vector_resident'] for p in pages),
               'coordination_resident_pages': sum(p['coordination_resident'] for p in pages),
               'initial_token_bytes': initial_bytes,
               'peak_token_bytes': max([initial_bytes] + [p['token_bytes'] for p in pages]),
               'token_byte_pages': sum(p['token_bytes'] for p in pages)}
    for name in ('hot', 'backup', 'heads', 'redirects', 'vector', 'coordination', 'total'):
        metrics['peak_' + name + '_bytes'] = max(p[name + '_bytes'] for p in pages)
        metrics[name + '_byte_pages'] = sum(p[name + '_bytes'] for p in pages)
    return {'inputs': inputs, 'responses': responses, 'pages': pages, 'summary': metrics}


def failures(service_type, adapter, params, baseline, threshold, control, recovery=True):
    """Fault both transitions, replay across workers and reject missing exact state."""
    tape = ValidatedSelection(adapter, baseline)
    service = service_type(tape, threshold=threshold, store=Store(recovery=recovery))
    token = service.start(chain='two-way-comparison')
    handles = [p['request'] for p in control['pages'] if p['input_kind'] == 'handle']
    switches = set(control['summary']['spill_requests'] + control['summary']['return_inline_requests'])
    fault_points = switches | set(choose_points(handles, 3))
    # Exercise a managed-inline continuation after a return, even if no re-spill.
    fault_points |= {n + 1 for n in control['summary']['return_inline_requests'] if n < len(control['pages'])}
    result = {name: 0 for name in ('hot_evictions', 'lost_responses', 'before_commit_failures',
                                  'concurrent_identical_commits', 'fresh_worker_identical_retries',
                                  'total_state_loss_rejections', 'stale_switch_input_rejections',
                                  'inline_replays_without_vectors')}
    stale_after = {}
    for number, expected_response in enumerate(control['responses'], 1):
        incoming = service.inspect(token)
        if incoming['kind'] == 'handle':
            if number in fault_points:
                lost = clone_store(service.store)
                lost.lose_state(incoming['chain'])
                fresh = service_type(tape, threshold=threshold, store=lost)
                try:
                    fresh.request(token, **params)
                except MissingState:
                    result['total_state_loss_rejections'] += 1
                else:
                    raise AssertionError('exact handle continued with both vectors missing')
            if recovery:
                service.store.drop_hot(incoming['chain'])
                result['hot_evictions'] += 1
        original_token = token
        if number in fault_points:
            a = service.prepare(token, **params)
            contender = service_type(tape, threshold=threshold, store=service.store)
            b = contender.prepare(token, **params)
            before = service.store.snapshot()
            try:
                service.commit(a, fault='before_commit')
            except InjectedFailure:
                result['before_commit_failures'] += 1
            else:
                raise AssertionError('before-commit injection did not fail')
            require(service.store.snapshot() == before, 'failed commit changed stored state')
            try:
                service.commit(a, fault='after_commit')
            except LostResponse:
                result['lost_responses'] += 1
            else:
                raise AssertionError('lost-response injection did not fail')
            accepted = service.store.snapshot()
            concurrent = contender.commit(b)
            require(canonical(concurrent) == canonical(expected_response), 'concurrent commit changed accepted response')
            require(service.store.snapshot() == accepted, 'concurrent retry changed accepted records')
            result['concurrent_identical_commits'] += 1
            service = service_type(tape, threshold=threshold, store=service.store)
            response = service.request(token, **params)
            require(service.store.snapshot() == accepted, 'fresh-worker retry changed accepted records')
            result['fresh_worker_identical_retries'] += 1
            if number in control['summary']['return_inline_requests']:
                require(not service.store.hot and not service.store.backup,
                        'returned inline replay retained separate vector copies')
                result['inline_replays_without_vectors'] += 1
            if number in switches and number < len(control['pages']):
                stale_after[number + 1] = original_token
        else:
            response = service.request(token, **params)
        require(canonical(response) == canonical(expected_response), 'fault path changed complete response')
        assert_page(service, response, baseline['pages'][number - 1])
        if number in stale_after:
            before = service.store.snapshot()
            try:
                service.request(stale_after[number], **params)
            except PlacementError:
                result['stale_switch_input_rejections'] += 1
            else:
                raise AssertionError('old switch input continued after replay window advanced')
            require(service.store.snapshot() == before, 'rejected stale input changed accepted storage')
        token = response['continuation']
    require(token is None, 'fault traversal did not complete')
    result.update(requests=len(control['responses']), fault_requests=sorted(fault_points),
                  byte_identical=True, all_checks_passed=True)
    return result


def paired_case(adapter, params, baseline, threshold, recovery=True):
    controls = {name: healthy(cls, adapter, params, baseline, threshold, recovery)
                for name, cls in (('one_way', OneWay), ('two_way', TwoWay))}
    for left, right in zip(controls['one_way']['responses'], controls['two_way']['responses']):
        for field in ('rows', 'work', 'status', 'client_action', 'chain', 'revision'):
            require(left[field] == right[field], 'two-way changed non-token response field: ' + field)
    results = {}
    for name, cls in (('one_way', OneWay), ('two_way', TwoWay)):
        control = controls[name]
        results[name] = {'summary': control['summary'], 'pages': control['pages'],
                         'failures': failures(cls, adapter, params, baseline, threshold, control, recovery)}
    before, after = results['one_way']['summary'], results['two_way']['summary']
    saved = {name: before[name] - after[name] for name in (
        'vector_resident_pages', 'coordination_resident_pages', 'vector_byte_pages',
        'coordination_byte_pages', 'total_byte_pages', 'token_byte_pages')}
    return {'threshold_bytes': threshold, 'backup_enabled': recovery,
            'one_way': results['one_way'], 'two_way': results['two_way'],
            'saved': saved, 'logical_results_identical': True, 'all_checks_passed': True}
