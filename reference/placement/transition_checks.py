"""Long-chain placement checks using the unchanged local protocol and public pager.

The published selection model is run and independently audited once per fixed
plan/encoding/request schedule. Exact protocol paths then replay those immutable
selection transitions, keyed by the complete incoming logical state and request
parameters. Restart tails use that same audited selection tape. Best-effort tails
memoize actual raw-tail adapter calls by full cursor and request parameters and
remain checked against independently constructed raw results. These are correctness
and serialized-token checks, not engine or latency benchmarks.
"""
from collections import Counter
from collections import defaultdict
from copy import deepcopy

from reference.grouped.audit import audit_boundary as grouped_audit

from .adapters import parameters
from .checks import canonical, represented_differences
from .protocol import InjectedFailure, LostResponse, Service


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def traversal_bound(adapter):
    sizes = [len(stage.candidate_ids if adapter.grouped else stage.items)
             for stage in adapter.plan.stages]
    return sum(sizes) * 2 + len(adapter.records) + len(sizes) + 10


def prepare_baseline(adapter, params):
    """Select and independently audit every boundary, caching no protocol state."""
    params = parameters(params)
    expected = adapter.expected_rows()
    expected_grouped = [(stage, identity, tuple(members)) for stage, identity, members in expected]
    state, history, pages, transitions = None, [], [], {}
    for _ in range(traversal_bound(adapter)):
        before = canonical(state)
        page = adapter.select(state, params)
        require(canonical(state) == before, 'published pager mutated its incoming state')
        history.extend(page['rows'])
        require(history == expected[:len(history)], 'published pager differs from oracle prefix')
        if adapter.grouped:
            grouped_audit(adapter.plan, page['state'], history, expected=expected_grouped)
        else:
            adapter.audit(page['state'], history)
        require(before not in transitions, 'logical continuation failed to make finite progress')
        transitions[before] = deepcopy(page)
        pages.append(deepcopy(page))
        state = page['state']
        if state is None:
            break
    else:
        raise AssertionError('published selection exceeded its finite traversal bound')
    require(history == expected, 'published pager differs from complete oracle')
    return {'binding': adapter.binding, 'plan_fingerprint': adapter.plan.fingerprint,
            'representation': adapter.representation, 'params': params,
            'expected': expected, 'pages': pages, 'transitions': transitions,
            'logical_boundaries_audited': len(pages), 'raw_tail_cache': {}}


def validate_baseline(adapter, params, baseline):
    require(baseline['binding'] == adapter.binding, 'baseline adapter binding differs')
    require(baseline['plan_fingerprint'] == adapter.plan.fingerprint, 'baseline plan differs')
    require(baseline['representation'] == adapter.representation, 'baseline encoding differs')
    require(baseline['params'] == parameters(params), 'baseline request parameters differ')
    require(bool(baseline['pages']), 'baseline contains no terminal response')
    require(baseline['pages'][-1]['state'] is None, 'baseline is incomplete')
    require(len(baseline['pages']) <= traversal_bound(adapter), 'baseline exceeds finite bound')


class ValidatedSelection:
    """A repeatable pure transition tape; no accepted protocol state is cached."""
    def __init__(self, adapter, baseline):
        validate_baseline(adapter, baseline['params'], baseline)
        self.adapter, self.baseline = adapter, baseline
        self.calls = 0
        self.raw_calls = self.raw_cache_misses = 0

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def select(self, state, params):
        require(parameters(params) == self.baseline['params'], 'selection tape parameters differ')
        key = canonical(state)
        require(key in self.baseline['transitions'], 'selection tape received an unvalidated state')
        self.calls += 1
        return deepcopy(self.baseline['transitions'][key])

    def best_effort(self, cursor, params):
        require(parameters(params) == self.baseline['params'], 'raw-tail cache parameters differ')
        self.adapter.validate_cursor(cursor)
        key = canonical(cursor)
        cache = self.baseline['raw_tail_cache']
        self.raw_calls += 1
        if key not in cache:
            cache[key] = deepcopy(self.adapter.best_effort(cursor, params))
            self.raw_cache_misses += 1
        return deepcopy(cache[key])


def _worker(adapter, threshold, policy='exact', store=None):
    return Service(adapter, threshold=threshold, policy=policy, store=store)


def _state(service, token):
    return None if token is None else service.logical_state(token)


def _assert_response(service, response, expected, *, status='exact', action='append'):
    require(response['rows'] == expected['rows'], 'protocol changed the validated rows')
    require(response['work'] == expected['work'], 'protocol changed validated selection work')
    require(_state(service, response['continuation']) == expected['state'],
            'protocol changed the validated logical continuation')
    require(response['status'] == status, 'protocol reported the wrong result contract')
    require(response['client_action'] == action, 'protocol reported the wrong client list action')


def healthy_run(adapter, threshold, params, baseline):
    tape = ValidatedSelection(adapter, baseline)
    service = _worker(tape, threshold)
    token = service.start(chain='transition-chain')
    initial_bytes = len(token.encode('ascii'))
    responses, inputs, pages, handles = [], [], [], []
    first_spill, selected, ever_stored = None, 0, False
    for number, expected in enumerate(baseline['pages'], 1):
        incoming = service.inspect(token)
        inputs.append(token)
        if incoming['kind'] == 'handle':
            handles.append(number)
        response = service.request(token, **params)
        _assert_response(service, response, expected)
        token = response['continuation']
        kind = 'terminal' if token is None else service.inspect(token)['kind']
        token_bytes = 0 if token is None else len(token.encode('ascii'))
        require(token_bytes <= threshold, 'complete outgoing token exceeded threshold')
        selected += len(response['rows'])
        if kind == 'handle':
            if first_spill is None:
                first_spill = {'request': number, 'rows_so_far': selected}
            ever_stored = True
        require(not ever_stored or kind != 'inline', 'ordinary chain returned inline after spilling')
        pages.append({'request': number, 'rows': len(response['rows']), 'rows_so_far': selected,
                      'input_kind': incoming['kind'], 'kind': kind, 'token_bytes': token_bytes,
                      'server_bytes': service.store.storage_bytes()})
        responses.append(response)
    require(token is None, 'healthy placement did not terminate')
    counts = Counter(page['kind'] for page in pages)
    return {'responses': responses, 'inputs': inputs, 'pages': pages,
            'handle_input_requests': handles,
            'summary': {'requests': len(pages), 'rows': selected,
                        'single_response': len(pages) == 1,
                        'inline_pages': counts['inline'], 'spilled_pages': counts['handle'],
                        'terminal_pages': counts['terminal'], 'handle_requests': len(handles),
                        'first_spill': first_spill,
                        'first_response_spill': first_spill is not None and first_spill['request'] == 1,
                        'mid_chain_switch': first_spill is not None and first_spill['request'] > 1,
                        'multiple_handle_responses': counts['handle'] >= 2,
                        'initial_token_bytes': initial_bytes,
                        'peak_token_bytes': max([initial_bytes] + [page['token_bytes'] for page in pages]),
                        'peak_server_bytes': max(page['server_bytes'] for page in pages),
                        'oracle_match': True,
                        'logical_boundaries_audited': baseline['logical_boundaries_audited'],
                        'cached_selection_calls': tape.calls}}


def choose_points(candidates, count=3):
    require(type(count) is int and count >= 0, 'loss_points must be a nonnegative integer')
    if not candidates or not count:
        return []
    if count == 1:
        return [candidates[0]]
    return sorted({candidates[index * (len(candidates) - 1) // (count - 1)]
                   for index in range(count)})


def exact_failure_run(adapter, threshold, params, baseline, healthy, loss_points=3):
    """Recover every hot miss, and lose responses at first spill and later handles."""
    tape = ValidatedSelection(adapter, baseline)
    service = _worker(tape, threshold)
    token = service.start(chain='transition-chain')
    fault_points = set(choose_points(healthy['handle_input_requests'], loss_points))
    first = healthy['summary']['first_spill']
    if first is not None:
        fault_points.add(first['request'])
    evictions = retries = before_faults = lost_responses = 0
    first_spill_replay = False
    handle_faults = 0
    for number, expected in enumerate(healthy['responses'], 1):
        incoming = service.inspect(token)
        if incoming['kind'] == 'handle':
            service.store.drop_hot(incoming['chain'])
            evictions += 1
        retry_input = token
        if number in fault_points:
            proposal = service.prepare(token, **params)
            before = service.store.snapshot()
            try:
                service.commit(proposal, fault='before_commit')
            except InjectedFailure:
                before_faults += 1
            else:
                raise AssertionError('before-commit injection did not fail')
            require(service.store.snapshot() == before, 'failed commit changed accepted storage')
            try:
                service.commit(proposal, fault='after_commit')
            except LostResponse:
                lost_responses += 1
            else:
                raise AssertionError('accepted-response-loss injection did not fail')
            if incoming['kind'] == 'handle':
                handle_faults += 1
            accepted = service.store.snapshot()
            service = _worker(tape, threshold, store=service.store)
            response = service.request(retry_input, **params)
            retries += 1
            require(service.store.snapshot() == accepted, 'lost-response retry changed accepted storage')
            first_spill_replay |= first is not None and number == first['request']
        else:
            response = service.request(token, **params)
            if incoming['kind'] == 'handle':
                accepted = service.store.snapshot()
                service = _worker(tape, threshold, store=service.store)
                retry = service.request(retry_input, **params)
                retries += 1
                require(canonical(retry) == canonical(response), 'handle retry changed response')
                require(service.store.snapshot() == accepted, 'handle retry advanced accepted storage')
        require(canonical(response) == canonical(expected), 'exact fault path changed complete response')
        _assert_response(service, response, baseline['pages'][number - 1])
        token = response['continuation']
    require(token is None, 'exact fault path did not finish')
    require(evictions == len(healthy['handle_input_requests']), 'not every handle saw a hot eviction')
    require(first is None or first_spill_replay, 'first-spill response loss was not exercised')
    return {'requests': len(healthy['responses']), 'hot_evictions': evictions,
            'identical_retries': retries, 'before_commit_failures': before_faults,
            'lost_responses': lost_responses, 'fault_requests': sorted(fault_points),
            'first_spill_lost_response_replayed': first_spill_replay,
            'handle_lost_responses': handle_faults, 'fresh_worker_retries': retries,
            'byte_identical_responses': True, 'cached_selection_calls': tape.calls}


def _raw_tail(adapter, cursor):
    records = sorted(adapter.records, key=lambda record: (record.sort_key, record.id), reverse=True)
    return [['best_effort', record.id, [record.id]] for record in records
            if cursor is None or (record.sort_key, record.id) < tuple(cursor)]


def fallback_failure_run(adapter, threshold, params, baseline, healthy, request_number, policy):
    """Exercise recovery and lost replies with independently checked pure selection."""
    require(policy in ('best_effort', 'restart'), 'unknown fallback policy')
    require(request_number in healthy['handle_input_requests'], 'total-loss point is not a handle input')
    tape = ValidatedSelection(adapter, baseline)
    service = _worker(tape, threshold, policy)
    token = service.start(chain='transition-chain')
    prefix = []
    for number in range(1, request_number):
        response = service.request(token, **params)
        _assert_response(service, response, baseline['pages'][number - 1])
        require(response['rows'] == healthy['responses'][number - 1]['rows'], 'pre-loss selection changed')
        prefix.extend(response['rows'])
        token = response['continuation']
    incoming = service.inspect(token)
    require(incoming['kind'] == 'handle', 'total-loss injection missed a handle')
    service.store.lose_state(incoming['chain'])
    # Restart repeats the validated original selection exactly. Raw fallback
    # selection is evaluated by the actual adapter once per complete cursor and
    # parameter set, then deep-copy memoized. Protocol state is never memoized.
    service = _worker(tape, threshold, policy, service.store)
    proposal = service.prepare(token, **params)
    before = service.store.snapshot()
    try:
        service.commit(proposal, fault='before_commit')
    except InjectedFailure:
        pass
    else:
        raise AssertionError('fallback before-commit injection did not fail')
    require(service.store.snapshot() == before, 'failed fallback published a replacement')
    try:
        service.commit(proposal, fault='after_commit')
    except LostResponse:
        pass
    else:
        raise AssertionError('fallback response-loss injection did not fail')
    accepted = service.store.snapshot()
    service = _worker(tape, threshold, policy, service.store)
    first = service.request(token, **params)
    require(service.store.snapshot() == accepted, 'fallback retry changed accepted storage')
    require(canonical(first) == canonical(proposal.response), 'lost fallback response changed on retry')
    require(canonical(service.request(token, **params)) == canonical(first), 'second fallback retry differs')
    require(first['status'] == policy, 'fallback failed to declare its result contract')
    require(first['chain'] != incoming['chain'], 'fallback reused the lost chain identity')
    require(first['client_action'] == ('replace' if policy == 'restart' else 'append'),
            'fallback reported the wrong client action')
    expected_tail = baseline['expected'] if policy == 'restart' else _raw_tail(adapter, incoming['anchor'])
    response, tail, count, stored_inputs = first, [], 0, 0
    for index in range(traversal_bound(adapter)):
        count += 1
        tail.extend(response['rows'])
        require(tail == expected_tail[:len(tail)], 'recovery tail differs from its independent result')
        continuation = response['continuation']
        if policy == 'restart':
            require(index < len(baseline['pages']), 'restart exceeded original complete traversal')
            _assert_response(service, response, baseline['pages'][index],
                             status='restart' if index == 0 else 'exact',
                             action='replace' if index == 0 else 'append')
        else:
            require(response['status'] == 'best_effort', 'best-effort tail lost its degraded label')
            require(response['client_action'] == 'append', 'best-effort tail replaced the client list')
            expected_state = (None if len(tail) == len(expected_tail)
                              else {'cursor': adapter.position(tail[-1][1])})
            require(_state(service, continuation) == expected_state, 'best-effort raw cursor differs')
        if continuation is None:
            break
        require(len(continuation.encode('ascii')) <= threshold, 'fallback token exceeded complete threshold')
        stored_inputs += service.inspect(continuation)['kind'] == 'handle'
        response = service.request(continuation, **params)
    else:
        raise AssertionError('recovery tail exceeded finite traversal bound')
    require(tail == expected_tail, 'recovery omitted part of its declared tail')
    require(policy != 'restart' or count == len(baseline['pages']), 'restart terminated on a different boundary')
    visible = tail if policy == 'restart' else prefix + tail
    return {'policy': policy, 'loss_before_request': request_number,
            'rows_before_loss': len(prefix), 'anchor': incoming['anchor'],
            'post_loss_requests': count, 'post_loss_rows': len(tail),
            'post_loss_handle_requests': stored_inputs,
            'before_commit_failures': 1, 'lost_responses': 1,
            'identical_retries': 2, 'fresh_worker_retries': 1,
            'client_action': first['client_action'], 'new_chain': True,
            'retry_identical': True, 'declared_result_match': True,
            'recovery_selection_calls': count,
            'recovery_selection_tape_calls': count if policy == 'restart' else 0,
            'actual_raw_adapter_calls': tape.raw_cache_misses,
            'memoized_raw_adapter_calls': tape.raw_calls - tape.raw_cache_misses,
            'visible_rows': len(visible),
            **represented_differences(baseline['expected'], visible)}


def run_transition_case(adapter, threshold, params, *, baseline=None, loss_points=3):
    params = parameters(params)
    baseline = prepare_baseline(adapter, params) if baseline is None else baseline
    validate_baseline(adapter, params, baseline)
    healthy = healthy_run(adapter, threshold, params, baseline)
    exact = exact_failure_run(adapter, threshold, params, baseline, healthy, loss_points)
    chosen = choose_points(healthy['handle_input_requests'], loss_points)
    fallbacks = [fallback_failure_run(adapter, threshold, params, baseline, healthy, number, policy)
                 for number in chosen for policy in ('best_effort', 'restart')]
    return {'threshold_bytes': threshold, 'params': params,
            'healthy': healthy['summary'], 'pages': healthy['pages'],
            'exact_recovery': exact, 'fallbacks': fallbacks,
            'loss_requests': chosen, 'individual_traversals': 2 + len(fallbacks),
            'all_checks_passed': True,
            'selection_execution': 'Audited public baseline; strict immutable state/parameter tape for exact and restart paths; actual raw adapter memoized by complete cursor and parameters for best-effort tails'}


# Reporting helpers shared by the transition witnesses and public comparisons.


def calibrated_threshold(probe):
    """Keep the first returned vector inline, then require at least two handles."""
    pages = probe['pages']
    if len(pages) < 4 or pages[0]['kind'] == 'terminal':
        return None
    threshold = max(probe['summary']['initial_token_bytes'], pages[0]['token_bytes'])
    for index, page in enumerate(pages[1:], 1):
        if page['token_bytes'] > threshold:
            remaining = sum(item['kind'] != 'terminal' for item in pages[index:])
            return threshold if remaining >= 2 else None
    return None


def summarize(results):
    groups = defaultdict(list)
    for result in results:
        category = 'grouped_2400' if result['large_grouped'] and result['scoped_records'] == 2400 else (
            'grouped_other_size' if result['large_grouped'] else 'small_and_core_controls')
        groups[(result['matrix'], category, result['page_size'], result['threshold_bytes'])].append(result)
    rows = []
    for (matrix, category, page_size, threshold), cases in sorted(groups.items()):
        rows.append({'matrix': matrix, 'category': category, 'page_size': page_size,
                     'threshold_bytes': threshold, 'cases': len(cases),
                     'single_response_cases': sum(c['healthy']['single_response'] for c in cases),
                     'first_response_spill_cases': sum(c['healthy']['first_response_spill'] for c in cases),
                     'mid_chain_switch_cases': sum(c['healthy']['mid_chain_switch'] for c in cases),
                     'multiple_handle_response_cases': sum(c['healthy']['multiple_handle_responses'] for c in cases),
                     'never_spilled_cases': sum(c['healthy']['first_spill'] is None for c in cases)})
    fallbacks = [entry for case in results for entry in case['fallbacks']]
    return {'cases': len(results), 'individual_protocol_traversals': sum(r['individual_traversals'] for r in results),
            'healthy_requests': sum(r['healthy']['requests'] for r in results),
            'hot_evictions': sum(r['exact_recovery']['hot_evictions'] for r in results),
            'exact_identical_retries': sum(r['exact_recovery']['identical_retries'] for r in results),
            'exact_lost_responses': sum(r['exact_recovery']['lost_responses'] for r in results),
            'exact_before_commit_failures': sum(r['exact_recovery']['before_commit_failures'] for r in results),
            'first_spill_lost_response_replays': sum(r['exact_recovery']['first_spill_lost_response_replayed'] for r in results),
            'fallback_traversals': len(fallbacks),
            'fallback_identical_retries': sum(f['identical_retries'] for f in fallbacks),
            'fallback_lost_responses': sum(f['lost_responses'] for f in fallbacks),
            'fallback_before_commit_failures': sum(f['before_commit_failures'] for f in fallbacks),
            'fallback_requests': sum(f['post_loss_requests'] for f in fallbacks),
            'single_response_cases': sum(r['healthy']['single_response'] for r in results),
            'mid_chain_switch_cases': sum(r['healthy']['mid_chain_switch'] for r in results),
            'multiple_handle_response_cases': sum(r['healthy']['multiple_handle_responses'] for r in results),
            'all_checks_passed': all(r['all_checks_passed'] for r in results),
            'coverage': rows}
