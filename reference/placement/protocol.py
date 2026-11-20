"""Fixed-data placement reference protocol with explicit recovery semantics.

The dictionaries are simulated failure domains, not durable storage. A lock models
atomic conditional publication. No live authorization, time-based expiry, engine
snapshots, distribution or production cryptographic key management is implemented.
"""
from dataclasses import dataclass
import base64
import copy
import hashlib
import hmac
import json
import threading

from .adapters import parameters


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('ascii')).hexdigest()


def token_hash(token):
    return hashlib.sha256(token.encode('ascii')).hexdigest()


class PlacementError(Exception):
    """Explicit protocol outcome; never interpreted as source exhaustion."""


class InvalidToken(PlacementError): pass
class MissingState(PlacementError): pass
class IntegrityError(PlacementError): pass
class StaleRevision(PlacementError): pass
class RevisionConflict(PlacementError): pass
class RegistryUnavailable(PlacementError): pass
class TokenCapacityError(PlacementError): pass
class InjectedFailure(PlacementError): pass
class LostResponse(PlacementError): pass


class Store:
    """Volatile vectors, optional independent checkpoints, and coordination ledger.

    The ledger retains the last accepted response and a head/tombstone per spilled
    chain. Losing it is a separate explicit failure. It is NOT reconstructed by
    treating a previously used inline token as a new chain.
    """
    def __init__(self, recovery=True):
        self.recovery = bool(recovery)
        self.hot = {}
        self.backup = {}
        self.heads = {}
        self.redirects = {}
        self.available = True
        self.lock = threading.RLock()

    def drop_hot(self, chain):
        with self.lock:
            self.hot.pop(chain, None)

    def drop_backup(self, chain):
        with self.lock:
            self.backup.pop(chain, None)

    def lose_state(self, chain):
        with self.lock:
            self.hot.pop(chain, None)
            self.backup.pop(chain, None)

    def forget_replay(self, chain):
        with self.lock:
            self.heads[chain]['response'] = None

    def lose_registry(self):
        with self.lock:
            self.available = False

    def snapshot(self):
        with self.lock:
            return copy.deepcopy({name: getattr(self, name)
                                  for name in ('hot', 'backup', 'heads', 'redirects')})

    def storage_bytes(self, chain=None):
        """Canonical serialized records across all modeled stores; not RAM size."""
        data = self.snapshot()
        return sum(len(canonical({'chain': key, 'record': value}).encode('ascii'))
                   for table in data.values() for key, value in table.items()
                   if chain is None or key == chain)


@dataclass(frozen=True)
class Proposal:
    kind: str
    input_chain: str
    expected_head: dict | None
    request_key: str
    input_token_hash: str
    input_revision: int
    response: dict
    checkpoint: dict | None = None
    persist: bool = False


class Service:
    """Dynamic inline-or-spill wrapper; the underlying pager remains unchanged."""
    def __init__(self, adapter, threshold=2048, policy='exact', store=None,
                 secret=b'local-test-key', reader='reader-1'):
        if type(threshold) is not int or threshold < 1:
            raise ValueError('Threshold must be a positive byte count')
        if policy not in ('exact', 'best_effort', 'restart'):
            raise ValueError('Unknown recovery policy')
        if not isinstance(secret, bytes) or not secret or not isinstance(reader, str) or not reader:
            raise ValueError('Supply a test key and reader binding')
        self.adapter, self.threshold, self.policy = adapter, threshold, policy
        self.store = Store() if store is None else store
        self.secret = secret
        self.binding = digest({'adapter': adapter.binding, 'reader': reader})
        self.stats = {'restored': 0, 'replayed': 0, 'best_effort': 0, 'restarted': 0}

    def _encode(self, payload):
        body = base64.urlsafe_b64encode(canonical(payload).encode('ascii')).rstrip(b'=').decode('ascii')
        signature = hmac.new(self.secret, body.encode('ascii'), hashlib.sha256).hexdigest()
        return body + '.' + signature

    def inspect(self, token):
        try:
            if type(token) is not str or len(token) > 16000000:
                raise InvalidToken('Malformed or oversized token')
            body, signature = token.split('.')
            expected = hmac.new(self.secret, body.encode('ascii'), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise InvalidToken('Token authentication failed')
            raw = base64.b64decode(body + '=' * (-len(body) % 4), altchars=b'-_', validate=True)
            payload = json.loads(raw)
            common = {'v', 'binding', 'chain', 'revision', 'mode', 'anchor', 'kind'}
            fields = common | ({'state'} if payload.get('kind') == 'inline' else {'digest'})
            if type(payload) is not dict or set(payload) != fields or payload['v'] != 1:
                raise InvalidToken('Unexpected token fields')
            if payload['binding'] != self.binding:
                raise InvalidToken('Reader, plan, scope or representation differs')
            if (type(payload['chain']) is not str or not payload['chain']
                    or type(payload['revision']) is not int or payload['revision'] < 0
                    or payload['kind'] not in ('inline', 'handle')
                    or payload['mode'] not in ('exact', 'best_effort')):
                raise InvalidToken('Invalid continuation shape')
            self.adapter.validate_cursor(payload['anchor'])
            if payload['kind'] == 'handle' and (
                    type(payload['digest']) is not str or len(payload['digest']) != 64):
                raise InvalidToken('Missing state digest')
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
        token = self._encode({'v': 1, 'binding': self.binding, 'chain': chain,
                              'revision': 0, 'mode': 'exact', 'anchor': None,
                              'kind': 'inline', 'state': None})
        if len(token.encode('ascii')) > self.threshold:
            raise TokenCapacityError('Threshold cannot carry the initial token envelope')
        return token

    def _available(self):
        if not self.store.available:
            raise RegistryUnavailable('Transition history is unavailable; no implicit chain recreation')

    def _checkpoint(self, chain, head, *, restore=True):
        checkpoint = self.store.hot.get(chain)
        if checkpoint is None and restore:
            checkpoint = self.store.backup.get(chain)
            from_backup = checkpoint is not None
        else:
            from_backup = False
        if checkpoint is None:
            raise MissingState('The accepted vector has no recoverable checkpoint')
        try:
            if (type(checkpoint) is not dict
                    or set(checkpoint) != {'binding', 'chain', 'revision', 'mode', 'state', 'anchor'}
                    or type(checkpoint['revision']) is not int
                    or checkpoint['mode'] not in ('exact', 'best_effort')
                    or type(checkpoint['state']) is not dict):
                raise IntegrityError('Malformed stored checkpoint')
            self.adapter.validate_cursor(checkpoint['anchor'])
            if (checkpoint.get('chain') != chain or checkpoint.get('binding') != self.binding
                    or checkpoint.get('revision') != head['revision']
                    or digest(checkpoint) != head['digest']):
                raise IntegrityError('Checkpoint binding, revision or digest does not match the accepted head')
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as exc:
            raise IntegrityError('Stored checkpoint cannot be validated') from exc
        if from_backup:
            self.store.hot[chain] = copy.deepcopy(checkpoint)
            self.stats['restored'] += 1
        return copy.deepcopy(checkpoint)

    def logical_state(self, token):
        payload = self.inspect(token)
        if payload['kind'] == 'inline':
            return copy.deepcopy(payload['state'])
        with self.store.lock:
            self._available()
            head = self.store.heads.get(payload['chain'])
            if head is None:
                raise MissingState('Unknown stored chain')
            if head['revision'] != payload['revision']:
                raise StaleRevision('Debug lookup requires the current accepted continuation')
            if head['digest'] != payload['digest']:
                raise IntegrityError('Token digest differs from accepted state')
            return self._checkpoint(payload['chain'], head)['state']

    def _proposal(self, payload, params, key, token_digest, head, *, fallback=False):
        if fallback:
            if self.policy == 'exact':
                raise MissingState('Exact recovery requires the latest accepted checkpoint')
            chain = 'recovery-' + digest({'input': key, 'policy': self.policy})[:24]
            revision = 0
            if self.policy == 'best_effort':
                mode, state, status, action = 'best_effort', {'cursor': payload['anchor']}, 'best_effort', 'append'
            else:
                mode, state, status, action = 'exact', None, 'restart', 'replace'
            anchor = payload['anchor'] if mode == 'best_effort' else None
        else:
            chain, revision = payload['chain'], payload['revision']
            mode, state, anchor = payload['mode'], payload['state'], payload['anchor']
            status, action = mode, 'append'
        if mode == 'best_effort':
            cursor = None if state is None else state['cursor']
            page = self.adapter.best_effort(cursor, params)
        else:
            page = self.adapter.select(state, params)
        rows, state = copy.deepcopy(page['rows']), copy.deepcopy(page['state'])
        if rows:
            anchor = self.adapter.position(rows[-1][1])
        revision += 1
        checkpoint = {'binding': self.binding, 'chain': chain, 'revision': revision,
                      'mode': mode, 'state': state, 'anchor': anchor}
        persist = not fallback and payload['kind'] == 'handle'
        continuation = None
        if state is not None:
            candidate = self._encode({'v': 1, 'binding': self.binding, 'chain': chain,
                                      'revision': revision, 'mode': mode, 'anchor': anchor,
                                      'kind': 'inline', 'state': state})
            if not persist and len(candidate.encode('ascii')) <= self.threshold:
                continuation = candidate
            else:
                persist = True
                continuation = self._encode({'v': 1, 'binding': self.binding, 'chain': chain,
                                             'revision': revision, 'mode': mode, 'anchor': anchor,
                                             'kind': 'handle', 'digest': digest(checkpoint)})
                if len(continuation.encode('ascii')) > self.threshold:
                    raise TokenCapacityError('Threshold cannot carry the stored-handle envelope')
        response = {'rows': rows, 'continuation': continuation, 'work': copy.deepcopy(page['work']),
                    'status': status, 'client_action': action, 'chain': chain, 'revision': revision}
        return Proposal('fallback' if fallback else ('advance' if persist else 'inline'),
                        payload['chain'], copy.deepcopy(head), key, token_digest,
                        payload['revision'], response, checkpoint, persist)

    def prepare(self, token, *, page_size=50, batch_size=50, candidate_budget=1000):
        params = parameters({'page_size': page_size, 'batch_size': batch_size,
                             'candidate_budget': candidate_budget})
        payload = self.inspect(token)
        token_digest = token_hash(token)
        key = digest({'token': token_digest, 'params': params, 'policy': self.policy})
        chain = payload['chain']
        fallback = False
        with self.store.lock:
            self._available()
            redirect = self.store.redirects.get(chain)
            if redirect is not None:
                if redirect['request_key'] != key:
                    raise StaleRevision('The lost chain has already been replaced')
                return Proposal('replay', chain, None, key, token_digest,
                                payload['revision'], copy.deepcopy(redirect['response']))
            head = copy.deepcopy(self.store.heads.get(chain))
            if head is None and (chain in self.store.hot or chain in self.store.backup):
                raise RegistryUnavailable('Checkpoint survived but its coordination record is missing')
            if head is not None:
                previous_request = (payload['revision'] == head['input_revision']
                                    and token_digest == head['input_token_hash'])
                if previous_request:
                    if head['request_key'] != key:
                        raise RevisionConflict('Accepted revision used different request parameters')
                    if head['response'] is None:
                        raise StaleRevision('The accepted response is outside the replay window')
                    if not head['terminal']:
                        try:
                            self._checkpoint(chain, head, restore=self.policy == 'exact')
                        except MissingState:
                            fallback = True
                    if not fallback:
                        return Proposal('replay', chain, head, key, token_digest,
                                        payload['revision'], copy.deepcopy(head['response']))
                else:
                    if payload['revision'] < head['revision']:
                        raise StaleRevision('Old revisions cannot continue from latest state')
                    if payload['revision'] != head['revision'] or payload['kind'] != 'handle':
                        raise RevisionConflict('Continuation does not name the accepted stored revision')
                    if payload['digest'] != head['digest']:
                        raise IntegrityError('Handle digest differs from accepted state')
                    try:
                        checkpoint = self._checkpoint(chain, head, restore=self.policy == 'exact')
                        payload['state'] = checkpoint['state']
                        if (payload['mode'], payload['anchor']) != (checkpoint['mode'], checkpoint['anchor']):
                            raise IntegrityError('Handle metadata differs from stored state')
                    except MissingState:
                        fallback = True
            elif payload['kind'] == 'handle':
                fallback = True
        # Selection is tentative; no accepted progress is changed here.
        return self._proposal(payload, params, key, token_digest, head, fallback=fallback)

    def _install(self, proposal):
        checkpoint = proposal.checkpoint
        chain = checkpoint['chain']
        if checkpoint['state'] is None:
            self.store.hot.pop(chain, None)
            self.store.backup.pop(chain, None)
        else:
            self.store.hot[chain] = copy.deepcopy(checkpoint)
            if self.store.recovery:
                self.store.backup[chain] = copy.deepcopy(checkpoint)
        self.store.heads[chain] = {
            'revision': checkpoint['revision'], 'digest': digest(checkpoint),
            'input_revision': proposal.input_revision, 'input_token_hash': proposal.input_token_hash,
            'request_key': proposal.request_key, 'response': copy.deepcopy(proposal.response),
            'terminal': checkpoint['state'] is None,
        }

    def commit(self, proposal, *, fault=None):
        if not isinstance(proposal, Proposal) or fault not in (None, 'before_commit', 'after_commit'):
            raise ValueError('Supply a prepared proposal and a supported failure point')
        with self.store.lock:
            self._available()
            if fault == 'before_commit':
                raise InjectedFailure('Failure before atomic publication; retry the original token')
            if proposal.kind == 'replay':
                self.stats['replayed'] += 1
                response = copy.deepcopy(proposal.response)
            else:
                redirect = self.store.redirects.get(proposal.input_chain)
                current = self.store.heads.get(proposal.input_chain)
                if redirect is not None:
                    if redirect['request_key'] != proposal.request_key:
                        raise RevisionConflict('Another recovery transition already won')
                    response = copy.deepcopy(redirect['response'])
                    self.stats['replayed'] += 1
                elif current != proposal.expected_head:
                    if current is not None and current['request_key'] == proposal.request_key and current['response'] is not None:
                        response = copy.deepcopy(current['response'])
                        self.stats['replayed'] += 1
                    else:
                        raise RevisionConflict('Another revision won; no implicit advancement')
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

    def request(self, token, *, page_size=50, batch_size=50, candidate_budget=1000, fault=None):
        proposal = self.prepare(token, page_size=page_size, batch_size=batch_size,
                                candidate_budget=candidate_budget)
        return self.commit(proposal, fault=fault)
