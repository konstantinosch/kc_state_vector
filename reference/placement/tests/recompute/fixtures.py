"""Actual-selector instrumentation and explicit scripted protocol witnesses."""
from copy import deepcopy

from reference.model import Batch, Item, Plan, Stage
from reference.placement.adapters import Adapter
from reference.placement.tests.two_way.fixtures import ScriptedAdapter

PARAMS = {'page_size': 1, 'batch_size': 3, 'candidate_budget': 1000}


class CountingAdapter:
    """Every call invokes its delegate; neither inputs nor outputs are cached."""
    def __init__(self, delegate):
        self.delegate = delegate
        self.binding = delegate.binding
        self.calls = []
        self.fail_next = False
        self.change = None

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def _call(self, method, value, params):
        entry = {'method': method, 'input': deepcopy(value), 'params': deepcopy(params)}
        self.calls.append(entry)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError('Injected selector failure')
        result = getattr(self.delegate, method)(value, params)
        if self.change is not None:
            self.change(result)
        entry['work'] = deepcopy(result['work'])
        entry['rows'] = deepcopy(result['rows'])
        return result

    def select(self, state, params):
        return self._call('select', state, params)

    def best_effort(self, cursor, params):
        return self._call('best_effort', cursor, params)


class MutableCoreSource:
    """Deletion deliberately violates the bound fixed-data assumption for a retry."""
    def __init__(self):
        self.hidden = set()
        self.reads = 0

    def read(self, stage, cursor, limit):
        self.reads += 1
        remaining = tuple(item for item in stage.items if item.id not in self.hidden
                          and (cursor is None or item.position < cursor))
        return Batch(remaining[:limit], len(remaining) <= limit)


def actual_core(size=120, source=None):
    items = tuple(Item(f'item-{n:04d}', n) for n in range(size, 0, -1))
    plan = Plan((Stage('priority', items[::2]), Stage('rest', items)), scope='recompute-core')
    return CountingAdapter(Adapter(plan, source=source))


def scripted(sizes=(4000, 0, 0, 4000, 0, None)):
    return CountingAdapter(ScriptedAdapter(sizes))


def worker(service, **changes):
    from reference.placement.recompute import Service
    values = {'threshold': service.threshold, 'policy': service.policy, 'store': service.store}
    values.update(changes)
    return Service(service.adapter, **values)


def first_handle(service, chain='recompute-chain', params=PARAMS):
    token = service.start(chain)
    response = service.request(token, **params)
    if service.inspect(response['continuation'])['kind'] != 'handle':
        raise AssertionError('Fixture must spill at its first response')
    return token, response


def returned(service, chain='recompute-chain'):
    first_input, first = first_handle(service, chain)
    response = service.request(first['continuation'], **PARAMS)
    if service.inspect(response['continuation'])['kind'] != 'inline':
        raise AssertionError('Fixture must return inline on second response')
    return first_input, first, response
