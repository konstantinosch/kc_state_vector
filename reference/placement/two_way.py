"""Local hysteretic placement with a retained replay/coordination ledger.

The vector may return to the client. The chain does not become session-free:
after its first spill every accepted response still advances a small head and a
one-response replay record. That record includes the inline token when returned.
The imported Store models separate failure domains, not real durable storage.
"""

import base64
import copy
import hashlib
import hmac
import json

from reference.placement.adapters import parameters
from reference.placement.protocol import (
    Service as OneWayService, Store, Proposal, canonical, digest, token_hash,
    PlacementError, InvalidToken, MissingState, IntegrityError, StaleRevision,
    RevisionConflict, RegistryUnavailable, TokenCapacityError, InjectedFailure,
    LostResponse,
)


class Service(OneWayService):
    """Spill above T; return from a handle only strictly below T/2.

    ``managed`` is an authenticated historical bit, not the current location.
    It stays true after first spill or fallback so loss of that chain's ledger
    can be rejected even when neither vector copy should exist. A previously
    unmanaged token cannot itself reveal loss of its later transition record;
    the same reliable-ledger/explicit-outage assumption as the one-way model
    still applies at the first spill.

    Tokens bind the placement version, threshold and recovery policy. Workers
    must agree on these settings; changing them requires a new chain.
    """

    def __init__(self, adapter, threshold=2048, policy='exact', store=None,
                 secret=b'local-test-key', reader='reader-1'):
        super().__init__(adapter, threshold, policy, store, secret, reader)
        self.binding = digest({'adapter': adapter.binding, 'reader': reader,
                               'placement': 'two-way-v2', 'threshold': threshold,
                               'recovery_policy': policy})

    def inspect(self, token):
        try:
            if type(token) is not str or len(token) > 16000000:
                raise InvalidToken('Malformed or oversized token')
            body, signature = token.split('.')
            expected = hmac.new(self.secret, body.encode('ascii'), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise InvalidToken('Token authentication failed')
            raw = base64.b64decode(body + '=' * (-len(body) % 4),
                                   altchars=b'-_', validate=True)
            payload = json.loads(raw)
            if type(payload) is not dict:
                raise InvalidToken('Unexpected token shape')
            common = {'v', 'binding', 'chain', 'revision', 'mode', 'anchor', 'kind', 'managed'}
            fields = common | ({'state'} if payload.get('kind') == 'inline' else {'digest'})
            if set(payload) != fields or payload['v'] != 2:
                raise InvalidToken('Unexpected token fields or placement version')
            if payload['binding'] != self.binding:
                raise InvalidToken('Reader, plan, scope, representation or placement policy differs')
            if (type(payload['chain']) is not str or not payload['chain']
                    or type(payload['revision']) is not int or payload['revision'] < 0
                    or payload['kind'] not in ('inline', 'handle')
                    or payload['mode'] not in ('exact', 'best_effort')
                    or type(payload['managed']) is not bool):
                raise InvalidToken('Invalid continuation shape')
            self.adapter.validate_cursor(payload['anchor'])
            if payload['kind'] == 'handle' and (
                    not payload['managed'] or type(payload['digest']) is not str
                    or len(payload['digest']) != 64):
                raise InvalidToken('Handle needs a managed chain and state digest')
            if payload['kind'] == 'inline' and payload['state'] is not None and type(payload['state']) is not dict:
                raise InvalidToken('Inline vector must be an object')
            if self._encode(payload) != token:
                raise InvalidToken('Token is not canonical')
            return payload
        except PlacementError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
            raise InvalidToken('Malformed continuation') from exc

    def start(self, chain='chain-1'):
        if type(chain) is not str or not chain:
            raise ValueError('A nonempty chain identity is required')
        token = self._encode({'v': 2, 'binding': self.binding, 'chain': chain,
                              'revision': 0, 'mode': 'exact', 'anchor': None,
                              'kind': 'inline', 'state': None, 'managed': False})
        if len(token.encode('ascii')) > self.threshold:
            raise TokenCapacityError('Threshold cannot carry the initial token envelope')
        return token

    def _validate_current(self, payload, incoming_hash, head):
        if payload['revision'] < head['revision']:
            raise StaleRevision('Old revisions cannot continue from latest state')
        if (head['terminal'] or payload['revision'] != head['revision']
                or payload['kind'] != head['kind'] or not payload['managed']):
            raise RevisionConflict('Continuation does not name the accepted managed revision')
        if incoming_hash != head['current_token_hash']:
            raise IntegrityError('Continuation differs from the exact accepted token')
        if payload['kind'] == 'handle' and payload['digest'] != head['digest']:
            raise IntegrityError('Handle digest differs from accepted state')

    def _validate_replay(self, chain, head):
        """Detect inconsistent saved responses under the trusted-ledger model.

        Digests do not authenticate a maliciously replaced ledger. The model
        assumes reliable coordination, as does the original one-way protocol.
        Recovery redirects retain that original trusted-ledger contract.
        """
        response = head['response']
        try:
            if (type(response) is not dict
                    or digest(response) != head['response_digest']
                    or response['chain'] != chain
                    or response['revision'] != head['revision']):
                raise IntegrityError('Saved replay response differs from accepted head')
            token = response['continuation']
            if head['terminal']:
                if token is not None or head['kind'] != 'terminal':
                    raise IntegrityError('Terminal replay contains a continuation')
                return
            if token_hash(token) != head['current_token_hash']:
                raise IntegrityError('Saved replay token differs from accepted head')
            payload = self.inspect(token)
            self._validate_current(payload, token_hash(token), head)
            if payload['chain'] != chain:
                raise IntegrityError('Saved replay token names a different chain')
            if payload['kind'] == 'inline':
                checkpoint = {name: payload[name] for name in
                              ('binding', 'chain', 'revision', 'mode', 'state', 'anchor')}
                if digest(checkpoint) != head['digest']:
                    raise IntegrityError('Saved inline vector differs from accepted head')
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise IntegrityError('Malformed saved replay response') from exc

    def logical_state(self, token):
        payload = self.inspect(token)
        if not payload['managed']:
            return copy.deepcopy(payload['state'])
        with self.store.lock:
            self._available()
            head = self.store.heads.get(payload['chain'])
            if head is None:
                raise RegistryUnavailable('Managed chain has lost its coordination record')
            self._validate_current(payload, token_hash(token), head)
            if payload['kind'] == 'inline':
                return copy.deepcopy(payload['state'])
            return self._checkpoint(payload['chain'], head)['state']

    def _proposal(self, payload, params, key, token_digest, head, *, fallback=False):
        if fallback:
            if self.policy == 'exact':
                raise MissingState('Exact recovery requires the latest accepted checkpoint')
            chain = 'recovery-' + digest({'input': key, 'policy': self.policy})[:24]
            revision = 0
            managed = True
            if self.policy == 'best_effort':
                mode, state, status, action = 'best_effort', {'cursor': payload['anchor']}, 'best_effort', 'append'
            else:
                mode, state, status, action = 'exact', None, 'restart', 'replace'
            anchor = payload['anchor'] if mode == 'best_effort' else None
        else:
            chain, revision = payload['chain'], payload['revision']
            mode, state, anchor = payload['mode'], payload['state'], payload['anchor']
            status, action = mode, 'append'
            managed = payload['managed'] or head is not None
        page = (self.adapter.best_effort(None if state is None else state['cursor'], params)
                if mode == 'best_effort' else self.adapter.select(state, params))
        rows, state = copy.deepcopy(page['rows']), copy.deepcopy(page['state'])
        if rows:
            anchor = self.adapter.position(rows[-1][1])
        revision += 1
        checkpoint = {'binding': self.binding, 'chain': chain, 'revision': revision,
                      'mode': mode, 'state': state, 'anchor': anchor}
        continuation = None
        if state is not None:
            common = {'v': 2, 'binding': self.binding, 'chain': chain,
                      'revision': revision, 'mode': mode, 'anchor': anchor}
            candidate = self._encode({**common, 'kind': 'inline', 'state': state,
                                      'managed': managed})
            candidate_bytes = len(candidate.encode('ascii'))
            was_handle = not fallback and payload['kind'] == 'handle'
            carry_inline = (2 * candidate_bytes < self.threshold if was_handle
                            else candidate_bytes <= self.threshold)
            if carry_inline:
                continuation = candidate
            else:
                managed = True
                continuation = self._encode({**common, 'kind': 'handle',
                                             'digest': digest(checkpoint), 'managed': True})
                if len(continuation.encode('ascii')) > self.threshold:
                    raise TokenCapacityError('Threshold cannot carry the stored-handle envelope')
        # ``persist`` means publish the ledger, not keep a server vector. Every
        # managed advance (including inline and terminal) must be conditional.
        response = {'rows': rows, 'continuation': continuation,
                    'work': copy.deepcopy(page['work']), 'status': status,
                    'client_action': action, 'chain': chain, 'revision': revision}
        return Proposal('fallback' if fallback else ('advance' if managed else 'inline'),
                        payload['chain'], copy.deepcopy(head), key, token_digest,
                        payload['revision'], response, checkpoint, managed)

    def prepare(self, token, *, page_size=50, batch_size=50, candidate_budget=1000):
        params = parameters({'page_size': page_size, 'batch_size': batch_size,
                             'candidate_budget': candidate_budget})
        payload = self.inspect(token)
        incoming_hash = token_hash(token)
        key = digest({'token': incoming_hash, 'params': params, 'policy': self.policy})
        chain = payload['chain']
        fallback = False
        with self.store.lock:
            self._available()
            redirect = self.store.redirects.get(chain)
            if redirect is not None:
                if redirect['request_key'] != key:
                    raise StaleRevision('The lost chain has already been replaced')
                return Proposal('replay', chain, None, key, incoming_hash,
                                payload['revision'], copy.deepcopy(redirect['response']))
            head = copy.deepcopy(self.store.heads.get(chain))
            if head is None and (payload['managed'] or chain in self.store.hot or chain in self.store.backup):
                raise RegistryUnavailable('Managed chain has lost its coordination record')
            if head is not None:
                previous_request = (payload['revision'] == head['input_revision']
                                    and incoming_hash == head['input_token_hash'])
                if previous_request:
                    if head['request_key'] != key:
                        raise RevisionConflict('Accepted revision used different request parameters')
                    if head['response'] is None:
                        raise StaleRevision('The accepted response is outside the replay window')
                    self._validate_replay(chain, head)
                    # An inline replay carries its own vector. Requiring a
                    # checkpoint here would make the return transition unusable.
                    if head['kind'] == 'handle':
                        try:
                            self._checkpoint(chain, head, restore=self.policy == 'exact')
                        except MissingState:
                            fallback = True
                    if not fallback:
                        return Proposal('replay', chain, head, key, incoming_hash,
                                        payload['revision'], copy.deepcopy(head['response']))
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
            # Selection is tentative. Only commit publishes accepted progress.
        return self._proposal(payload, params, key, incoming_hash, head, fallback=fallback)

    def _install(self, proposal):
        checkpoint = proposal.checkpoint
        chain = checkpoint['chain']
        continuation = proposal.response['continuation']
        kind = 'terminal' if continuation is None else self.inspect(continuation)['kind']
        if kind == 'handle':
            self.store.hot[chain] = copy.deepcopy(checkpoint)
            if self.store.recovery:
                self.store.backup[chain] = copy.deepcopy(checkpoint)
            else:
                self.store.backup.pop(chain, None)
        else:
            # Delete both vector copies atomically with publication of the
            # replay response containing the new inline token (or terminal row).
            self.store.hot.pop(chain, None)
            self.store.backup.pop(chain, None)
        self.store.heads[chain] = {
            'revision': checkpoint['revision'], 'digest': digest(checkpoint),
            'input_revision': proposal.input_revision, 'input_token_hash': proposal.input_token_hash,
            'request_key': proposal.request_key, 'response': copy.deepcopy(proposal.response),
            'response_digest': digest(proposal.response),
            'terminal': checkpoint['state'] is None, 'kind': kind,
            'current_token_hash': None if continuation is None else token_hash(continuation),
        }
