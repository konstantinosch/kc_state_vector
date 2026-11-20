"""Two-way placement with compact, digest-verified inline-input replay.

Inline-input retries execute the actual selector. Handle-input transitions keep
their accepted full response as a bridge until the next successful publication.
The bridge is an alternative to retaining the pre-selection vector: the returned
vector alone cannot reconstruct the page just delivered. Handle-to-terminal
bridges remain until explicit replay expiry. No new storage durability is modeled.
"""
from dataclasses import dataclass, replace
import copy

from reference.placement.adapters import parameters
from reference.placement.protocol import (
    Store as BaseStore, Proposal as BaseProposal, canonical, digest, token_hash,
    PlacementError, InvalidToken, MissingState, IntegrityError, StaleRevision,
    RevisionConflict, RegistryUnavailable, TokenCapacityError, InjectedFailure,
    LostResponse,
)
from reference.placement.two_way import Service as FullReplayService


class RecomputeMismatch(IntegrityError):
    """The retried selection no longer reproduces its accepted response."""


class Store(BaseStore):
    """Same four modeled tables, with explicit compact-replay expiry."""
    def expire_replay(self, chain):
        with self.lock:
            self.heads[chain]['replay_expired'] = True
            self.heads[chain]['response'] = None

    def forget_replay(self, chain):
        self.expire_replay(chain)


@dataclass(frozen=True)
class Proposal(BaseProposal):
    input_kind: str = 'inline'


class Service(FullReplayService):
    """Same v2 wire layout and hysteresis, different bound replay strategy.

    The ledger remains authoritative even after returning inline. Digest checks
    establish equality to an accepted response, not deterministic future reads.
    The selector, plan/data version, all result-affecting request parameters and
    canonical response encoding must remain repeatable for recomputation to work.

    Full response bridges are retained for handle inputs and recovery redirects.
    Compact inline-input records contain no inline vector or result row list.
    A missing currently accepted handle checkpoint is never recreated from a
    previous inline token. The original explicit recovery policies still apply.
    """
    def __init__(self, adapter, threshold=2048, policy='exact', store=None,
                 secret=b'local-test-key', reader='reader-1'):
        super().__init__(adapter, threshold, policy,
                         Store() if store is None else store, secret, reader)
        self.binding = digest({'placement_binding': self.binding,
                               'replay_strategy': 'inline-recompute-v1'})
        self.stats['recomputed'] = 0

    def _proposal(self, payload, params, key, token_digest, head, *, fallback=False):
        base = super()._proposal(payload, params, key, token_digest, head,
                                 fallback=fallback)
        return Proposal(**vars(base), input_kind=payload['kind'])

    def _check_head_binding(self, head):
        required = {'binding', 'revision', 'digest', 'input_revision',
                    'input_token_hash', 'request_key', 'response', 'response_digest',
                    'terminal', 'kind', 'current_token_hash', 'replay_mode', 'replay_expired'}
        if type(head) is not dict or not required <= head.keys():
            raise IntegrityError('Accepted replay metadata is incomplete')
        def is_digest(value):
            return (type(value) is str and len(value) == 64
                    and all(character in '0123456789abcdef' for character in value))
        if (type(head['revision']) is not int or head['revision'] < 1
                or type(head['input_revision']) is not int or head['input_revision'] < 0
                or type(head['terminal']) is not bool
                or type(head['replay_expired']) is not bool
                or head['kind'] not in ('inline', 'handle', 'terminal')
                or head['terminal'] != (head['kind'] == 'terminal')
                or any(not is_digest(head[name]) for name in
                       ('binding', 'digest', 'input_token_hash', 'request_key', 'response_digest'))
                or (head['current_token_hash'] is not None if head['terminal']
                    else not is_digest(head['current_token_hash']))):
            raise IntegrityError('Accepted replay metadata is malformed')
        if head.get('binding') != self.binding:
            raise IntegrityError('Stored head binding differs from the replay strategy')
        if head.get('replay_mode') not in ('stored', 'recompute'):
            raise IntegrityError('Unknown accepted replay representation')
        if head['replay_mode'] == 'recompute' and head['response'] is not None:
            raise IntegrityError('Compact replay unexpectedly contains a stored response')

    def _require_live_replay(self, head):
        self._check_head_binding(head)
        if head.get('replay_expired', False) or (
                head['replay_mode'] == 'stored' and head.get('response') is None):
            raise StaleRevision('The accepted response is outside the replay window')

    def _validate_current(self, payload, incoming_hash, head):
        self._check_head_binding(head)
        return super()._validate_current(payload, incoming_hash, head)

    def _verify_recomputed(self, response, chain, head):
        """Verify all response fields before releasing a recomputed result."""
        if digest(response) != head['response_digest']:
            raise RecomputeMismatch('Recomputed response differs from the accepted response digest')
        if response['chain'] != chain or response['revision'] != head['revision']:
            raise IntegrityError('Recomputed response does not name the accepted revision')
        token = response['continuation']
        if head['terminal']:
            if token is not None or head['kind'] != 'terminal':
                raise IntegrityError('Terminal recomputation contains a continuation')
            return
        if token is None:
            raise IntegrityError('Recomputation lost the accepted continuation')
        payload = self.inspect(token)
        self._validate_current(payload, token_hash(token), head)
        if payload['chain'] != chain:
            raise IntegrityError('Recomputed continuation names another chain')
        if payload['kind'] == 'inline':
            checkpoint = {name: payload[name] for name in
                          ('binding', 'chain', 'revision', 'mode', 'state', 'anchor')}
            if digest(checkpoint) != head['digest']:
                raise IntegrityError('Recomputed inline vector differs from accepted state')

    def _require_current_vector(self, chain, head):
        if head['kind'] == 'handle':
            self._checkpoint(chain, head, restore=self.policy == 'exact')

    def prepare(self, token, *, page_size=50, batch_size=50, candidate_budget=1000):
        params = parameters({'page_size': page_size, 'batch_size': batch_size,
                             'candidate_budget': candidate_budget})
        payload = self.inspect(token)
        incoming_hash = token_hash(token)
        key = digest({'token': incoming_hash, 'params': params, 'policy': self.policy})
        chain = payload['chain']
        fallback = recompute = False
        with self.store.lock:
            self._available()
            redirect = self.store.redirects.get(chain)
            if redirect is not None:
                if redirect['request_key'] != key:
                    raise StaleRevision('The lost chain has already been replaced')
                return Proposal('redirect_replay', chain, None, key, incoming_hash,
                                payload['revision'], copy.deepcopy(redirect['response']),
                                input_kind=payload['kind'])
            head = copy.deepcopy(self.store.heads.get(chain))
            if head is None and (payload['managed'] or chain in self.store.hot or chain in self.store.backup):
                raise RegistryUnavailable('Managed chain has lost its coordination record')
            if head is not None:
                self._check_head_binding(head)
                previous_request = (payload['revision'] == head['input_revision']
                                    and incoming_hash == head['input_token_hash'])
                if previous_request:
                    if head['request_key'] != key:
                        raise RevisionConflict('Accepted revision used different request parameters')
                    self._require_live_replay(head)
                    try:
                        self._require_current_vector(chain, head)
                    except MissingState:
                        fallback = True
                    if not fallback:
                        if head['replay_mode'] == 'stored':
                            self._validate_replay(chain, head)
                            return Proposal('replay', chain, head, key, incoming_hash,
                                            payload['revision'], copy.deepcopy(head['response']),
                                            input_kind=payload['kind'])
                        if payload['kind'] != 'inline':
                            raise IntegrityError('Compact replay requires its original inline input')
                        recompute = True
                else:
                    self._validate_current(payload, incoming_hash, head)
                    if payload['kind'] == 'handle':
                        try:
                            checkpoint = self._checkpoint(chain, head, restore=self.policy == 'exact')
                            payload['state'] = checkpoint['state']
                            if (payload['mode'], payload['anchor']) != (checkpoint['mode'], checkpoint['anchor']):
                                raise IntegrityError('Handle metadata differs from stored state')
                        except MissingState:
                            fallback = True
        if recompute:
            # Reproduce the original placement context. A first-spill input was
            # unmanaged, even though its accepted head exists now. Changing that
            # bit can change bytes at the exact spill boundary.
            original_head = head if payload['managed'] else None
            self.stats['recomputed'] += 1
            candidate = self._proposal(payload, params, key, incoming_hash, original_head)
            self._verify_recomputed(candidate.response, chain, head)
            return replace(candidate, kind='recompute', expected_head=copy.deepcopy(head),
                           persist=False)
        # Actual selection is tentative and never releases a retained bridge.
        return self._proposal(payload, params, key, incoming_hash, head, fallback=fallback)

    def _install(self, proposal):
        super()._install(proposal)
        head = self.store.heads[proposal.checkpoint['chain']]
        head['binding'] = self.binding
        head['replay_expired'] = False
        stored = proposal.input_kind == 'handle' or proposal.kind == 'fallback'
        head['replay_mode'] = 'stored' if stored else 'recompute'
        if not stored:
            head['response'] = None

    def commit(self, proposal, *, fault=None):
        if not isinstance(proposal, Proposal) or fault not in (None, 'before_commit', 'after_commit'):
            raise ValueError('Supply a prepared recompute proposal and supported failure point')
        with self.store.lock:
            self._available()
            if fault == 'before_commit':
                raise InjectedFailure('Failure before atomic publication; retry the original token')
            redirect = self.store.redirects.get(proposal.input_chain)
            current = self.store.heads.get(proposal.input_chain)
            if proposal.kind == 'redirect_replay':
                if redirect is None or redirect['request_key'] != proposal.request_key:
                    raise StaleRevision('Accepted recovery replay is unavailable')
                response = copy.deepcopy(redirect['response'])
                self.stats['replayed'] += 1
            elif proposal.kind in ('replay', 'recompute'):
                # Selection may have run outside the lock. Do not return an old
                # replay after another accepted publication advances the window.
                if redirect is not None or current != proposal.expected_head:
                    raise StaleRevision('The accepted replay window advanced during retry')
                self._require_live_replay(current)
                self._require_current_vector(proposal.input_chain, current)
                if proposal.kind == 'recompute':
                    self._verify_recomputed(proposal.response, proposal.input_chain, current)
                    response = copy.deepcopy(proposal.response)
                else:
                    self._validate_replay(proposal.input_chain, current)
                    response = copy.deepcopy(current['response'])
                self.stats['replayed'] += 1
            elif redirect is not None:
                if redirect['request_key'] != proposal.request_key:
                    raise RevisionConflict('Another recovery transition already won')
                response = copy.deepcopy(redirect['response'])
                self.stats['replayed'] += 1
            elif current != proposal.expected_head:
                if current is None or current['request_key'] != proposal.request_key:
                    raise RevisionConflict('Another revision won; no implicit advancement')
                self._require_live_replay(current)
                self._require_current_vector(proposal.input_chain, current)
                if current['replay_mode'] == 'stored':
                    self._validate_replay(proposal.input_chain, current)
                    response = copy.deepcopy(current['response'])
                else:
                    # A simultaneous normal selection can serve as the
                    # recomputation only after it matches the accepted digest.
                    self._verify_recomputed(proposal.response, proposal.input_chain, current)
                    response = copy.deepcopy(proposal.response)
                self.stats['replayed'] += 1
            else:
                if proposal.persist:
                    self._install(proposal)
                if proposal.kind == 'fallback':
                    self.store.redirects[proposal.input_chain] = {
                        'request_key': proposal.request_key,
                        'response': copy.deepcopy(proposal.response),
                    }
                    self.stats['best_effort' if self.policy == 'best_effort' else 'restarted'] += 1
                response = copy.deepcopy(proposal.response)
            if fault == 'after_commit':
                raise LostResponse('Accepted response was lost; retry the original token')
            return response
