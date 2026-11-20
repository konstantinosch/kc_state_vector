"""Small scripted vectors isolate placement from selection correctness.

Padding represents already-decided opaque vector state. These fixtures make no
query-performance or retention claim; published selection traces run separately.
"""
from copy import deepcopy

from reference.placement.protocol import digest


PARAMS = {'page_size': 1, 'batch_size': 3, 'candidate_budget': 10}


class ScriptedAdapter:
    def __init__(self, sizes=(4000, 0, 3000, 0, None)):
        self.sizes = tuple(sizes)
        self.binding = digest({'scripted_vector_sizes': self.sizes})
        self.calls = 0

    def position(self, identity):
        index = int(identity.removeprefix('item-'))
        if identity != f'item-{index}' or not 0 <= index < len(self.sizes):
            raise ValueError('Unknown scripted ID')
        return [10000 - index, identity]

    def validate_cursor(self, cursor):
        if cursor is not None and (type(cursor) is not list or len(cursor) != 2
                                   or self.position(cursor[1]) != cursor):
            raise ValueError('Invalid scripted cursor')

    def select(self, state, params):
        self.calls += 1
        index = 0 if state is None else state['next']
        if not 0 <= index < len(self.sizes):
            raise ValueError('Script consumed beyond its terminal response')
        size = self.sizes[index]
        identity = f'item-{index}'
        return {'rows': [['script', identity, [identity]]],
                'state': None if size is None else {'next': index + 1, 'padding': 'x' * size},
                'work': {'consumed': 1, 'fetched': 1, 'reads': 1}}

    def best_effort(self, cursor, params):
        self.validate_cursor(cursor)
        index = 0 if cursor is None else int(cursor[1].removeprefix('item-')) + 1
        if index >= len(self.sizes):
            return {'rows': [], 'state': None, 'work': {'consumed': 0, 'fetched': 0, 'reads': 1}}
        identity = f'item-{index}'
        return {'rows': [['best_effort', identity, [identity]]],
                'state': None if index == len(self.sizes) - 1 else {'cursor': self.position(identity)},
                'work': {'consumed': 1, 'fetched': 1, 'reads': 1}}


def worker_like(service, **options):
    from reference.placement.two_way import Service
    return Service(service.adapter, threshold=service.threshold, policy=service.policy,
                   store=service.store, **options)


def first_handle(service, chain='chain-test'):
    token = service.start(chain)
    response = service.request(token, **PARAMS)
    if service.inspect(response['continuation'])['kind'] != 'handle':
        raise AssertionError('Fixture must spill on its first response')
    return token, response


def one_chain_snapshot(store, chain):
    return {table: deepcopy(rows.get(chain)) for table, rows in store.snapshot().items()}


def inline_size(adapter, index, managed):
    """Measure an actual signed inline response with only the marker changed.

The binding remains a fixed-length digest at every threshold. No production
threshold is calibrated with this helper; it creates exact boundary witnesses.
"""
    from reference.placement.two_way import Service
    service = Service(adapter, threshold=1000000)
    token = service.start('chain-test')
    for _ in range(index + 1):
        token = service.request(token, **PARAMS)['continuation']
    payload = service.inspect(token)
    payload['managed'] = managed
    return len(service._encode(payload).encode('ascii'))
