"""Small correct B3 example adapter and healthy control, not a production server.

Immutable fixture sources, raw-candidate budgets, explicit-ID coverage and safe
retirement. Source reads scan in-memory lists; there are no performance claims.
"""

import copy
import hashlib
import json


CAPABILITIES = {'batch_size', 'candidate_budget', 'stage_labels'}


def make_pager(fixture):
    """Construct an independent page callable; progress lives in its continuation."""
    # The harness validates fixtures. Keep selection independent of its oracle.
    stages = copy.deepcopy(fixture['stages'])
    positions = {record['id']: (record['sort_key'], record['id']) for record in fixture['records']}
    sources = [sorted((positions[identity] for identity in stage['ids']), reverse=True)
               for stage in stages]
    memberships = [set(stage['ids']) for stage in stages]
    binding = hashlib.sha256(json.dumps(fixture, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()

    def new_state():
        return {'binding': binding, 'stage': 0, 'cursors': [None] * len(stages),
                'selected': [0] * len(stages), 'coverage': []}

    def settle(state):
        while state['stage'] < len(stages):
            index = state['stage']
            quota = stages[index]['quota']
            if quota is None or state['selected'][index] < quota:
                break
            state['stage'] += 1

    def prune(state):
        state['coverage'] = [identity for identity in state['coverage'] if any(
            identity in memberships[index] and
            (state['cursors'][index] is None or positions[identity] < tuple(state['cursors'][index]))
            for index in range(state['stage'], len(stages))
        )]

    def page(continuation, *, page_size, batch_size=50, candidate_budget=1000):
        for value in (page_size, batch_size, candidate_budget):
            if type(value) is not int or not 1 <= value <= 1000:
                raise ValueError('Request limits must be integers from 1 through 1000')
        if continuation is None:
            state = new_state()
        else:
            state = copy.deepcopy(continuation)
            if type(state) is not dict or state.get('binding') != binding:
                raise ValueError('Continuation belongs to a different fixture')
        settle(state)
        prune(state)
        rows, consumed = [], 0
        while state['stage'] < len(stages) and len(rows) < page_size and consumed < candidate_budget:
            index = state['stage']
            cursor = state['cursors'][index]
            remaining = [position for position in sources[index]
                         if cursor is None or position < tuple(cursor)]
            batch = remaining[:min(batch_size, candidate_budget - consumed)]
            if not batch:
                state['stage'] += 1
                settle(state)
                prune(state)
                continue
            processed = 0
            for sort_key, identity in batch:
                if len(rows) == page_size or consumed == candidate_budget or state['stage'] != index:
                    break
                excluded = identity in state['coverage']
                state['cursors'][index] = [sort_key, identity]
                consumed += 1
                processed += 1
                if not excluded:
                    rows.append({'stage': stages[index]['name'], 'id': identity})
                    state['selected'][index] += 1
                    state['coverage'].append(identity)
                settle(state)
                prune(state)
            if state['stage'] == index and processed == len(batch) and len(batch) == len(remaining):
                state['stage'] += 1
                settle(state)
                prune(state)
        continuation = None if state['stage'] == len(stages) else state
        return {'rows': rows, 'continuation': continuation, 'consumed': consumed}

    return page
