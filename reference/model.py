"""Exact fixed-data selection; a logical reference, not a production token service.

Sources contain only stable identities and positions. Reservations are immutable
plan inputs, established before scanning. Returned dictionaries are trusted logical
state: a real transport adapter must authenticate, authorize and protect them.
"""

from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType


class PlanError(ValueError):
    pass


class StateError(ValueError):
    pass


class ReadError(RuntimeError):
    pass


class CapacityError(RuntimeError):
    pass


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


@dataclass(frozen=True)
class Item:
    id: str
    sort_key: int

    def __post_init__(self):
        if type(self.id) is not str or not self.id or type(self.sort_key) is not int:
            raise PlanError("An item needs a nonempty string ID and an integer sort key.")

    @property
    def position(self):
        return self.sort_key, self.id


@dataclass(frozen=True)
class Stage:
    name: str
    items: tuple[Item, ...]
    quota: int | None = None
    buffer_size: int | None = None

    def __post_init__(self):
        if type(self.name) is not str or not self.name:
            raise PlanError("A stage needs a nonempty name.")
        items = tuple(self.items)
        if not all(isinstance(item, Item) for item in items):
            raise PlanError("Stage items must be Item values.")
        if len({item.id for item in items}) != len(items):
            raise PlanError("Each source must yield each item once.")
        if self.quota is not None and not _integer(self.quota):
            raise PlanError("A stage quota is a nonnegative integer or None.")
        if self.buffer_size is not None and not _integer(self.buffer_size, 1):
            raise PlanError("A prepared buffer size must be positive.")
        object.__setattr__(self, "items", tuple(sorted(items, key=lambda item: item.position, reverse=True)))


@dataclass(frozen=True)
class Reservation:
    owner: str
    item_ids: tuple[str, ...]
    yield_from: tuple[str, ...] | None = None
    on_unserved: str = "omit"

    def __post_init__(self):
        if type(self.owner) is not str or not self.owner:
            raise PlanError("A reservation needs a nonempty owner name.")
        ids = tuple(self.item_ids)
        if any(type(item) is not str or not item for item in ids) or len(set(ids)) != len(ids):
            raise PlanError("A reservation needs distinct, nonempty string IDs.")
        if self.on_unserved not in ("omit", "release"):
            raise PlanError("Reservation completion must be 'omit' or 'release'.")
        object.__setattr__(self, "item_ids", ids)
        if self.yield_from is not None:
            names = tuple(self.yield_from)
            if any(type(name) is not str for name in names) or len(set(names)) != len(names):
                raise PlanError("Yielding stage names must be distinct strings.")
            object.__setattr__(self, "yield_from", names)


@dataclass(frozen=True)
class _Claim:
    owner: int
    yielding: frozenset[int]
    on_unserved: str


@dataclass(frozen=True)
class Plan:
    stages: tuple[Stage, ...]
    reservations: tuple[Reservation, ...] = ()
    scope: str = "default"
    _items: object = field(init=False, repr=False, compare=False)
    _members: tuple = field(init=False, repr=False, compare=False)
    _claims: object = field(init=False, repr=False, compare=False)
    fingerprint: str = field(init=False)

    def __post_init__(self):
        stages, reservations = tuple(self.stages), tuple(self.reservations)
        if not all(isinstance(stage, Stage) for stage in stages):
            raise PlanError("A plan contains Stage values.")
        if not all(isinstance(value, Reservation) for value in reservations):
            raise PlanError("A plan contains Reservation values.")
        names = {stage.name: i for i, stage in enumerate(stages)}
        if len(names) != len(stages):
            raise PlanError("Stage names must be unique.")
        if type(self.scope) is not str or not self.scope:
            raise PlanError("A plan requires a nonempty fixed scope.")
        items = {}
        members = []
        for stage in stages:
            members.append(frozenset(item.id for item in stage.items))
            for item in stage.items:
                if item.id in items and items[item.id] != item:
                    raise PlanError("Every occurrence of an ID must have the same position.")
                items[item.id] = item
        declared = set()
        for reservation in reservations:
            if reservation.owner not in names or reservation.owner in declared:
                raise PlanError("Each reservation has one known, uniquely declared owner.")
            declared.add(reservation.owner)
            if not set(reservation.item_ids) <= members[names[reservation.owner]]:
                raise PlanError("Reserved IDs must belong to the owner's source.")
            if reservation.yield_from is not None:
                if not set(reservation.yield_from) <= names.keys() or reservation.owner in reservation.yield_from:
                    raise PlanError("Yielding stages must be known non-owner stages.")
        claims = {}
        for reservation in sorted(reservations, key=lambda value: names[value.owner]):
            owner = names[reservation.owner]
            yielding = frozenset(names[name] for name in reservation.yield_from) if reservation.yield_from is not None else frozenset(i for i in range(len(stages)) if i != owner)
            for identity in reservation.item_ids:
                claims.setdefault(identity, _Claim(owner, yielding, reservation.on_unserved))
        signature = {
            "scope": self.scope,
            "stages": [{"name": stage.name, "items": [item.position for item in stage.items],
                        "quota": stage.quota, "buffer_size": stage.buffer_size} for stage in stages],
            "claims": {identity: [claim.owner, sorted(claim.yielding), claim.on_unserved]
                       for identity, claim in sorted(claims.items())},
        }
        object.__setattr__(self, "stages", stages)
        object.__setattr__(self, "reservations", reservations)
        object.__setattr__(self, "_items", MappingProxyType(items))
        object.__setattr__(self, "_members", tuple(members))
        object.__setattr__(self, "_claims", MappingProxyType(claims))
        object.__setattr__(self, "fingerprint", hashlib.sha256(_json(signature).encode()).hexdigest())


@dataclass(frozen=True)
class Batch:
    items: tuple[Item, ...]
    exhausted: bool
    complete: bool = True


class MemorySource:
    """Finite complete source; list scans deliberately make no performance promise."""

    def read(self, stage, cursor, limit):
        remaining = tuple(item for item in stage.items if cursor is None or item.position < cursor)
        return Batch(remaining[:limit], len(remaining) <= limit)


@dataclass(frozen=True)
class Page:
    rows: tuple[tuple[str, str], ...]
    state: dict | None
    consumed: int
    fetched: int
    reads: int

    @property
    def ids(self):
        return tuple(identity for _, identity in self.rows)


class Pager:
    def __init__(self, plan, source=None, *, max_state_entries=10000, max_state_bytes=1000000):
        if not isinstance(plan, Plan):
            raise PlanError("Pager needs a Plan.")
        for limit in (max_state_entries, max_state_bytes):
            if limit is not None and not _integer(limit):
                raise ValueError("State limits are nonnegative integers or None.")
        self.plan = plan
        self.source = source if source is not None else MemorySource()
        self.max_state_entries = max_state_entries
        self.max_state_bytes = max_state_bytes

    def _new_state(self):
        return {
            "v": 1, "plan": self.plan.fingerprint, "scope": self.plan.scope, "stage": 0,
            "cursors": [None for _ in self.plan.stages],
            "selected": [0 for _ in self.plan.stages],
            "coverage": [], "pending": sorted(self.plan._claims), "omitted": [],
            "buffers": [None if stage.buffer_size is None else
                        {"items": [], "fetch": None, "exhausted": False}
                        for stage in self.plan.stages],
        }

    def _check_capacity(self, vector):
        if vector["stage"] == len(self.plan.stages):
            return
        entries = sum(len(vector[key]) for key in ("coverage", "pending", "omitted"))
        entries += sum(len(buffer["items"]) for buffer in vector["buffers"] if buffer is not None)
        if self.max_state_entries is not None and entries > self.max_state_entries:
            raise CapacityError("Required coverage, reservation or buffer entries exceed capacity.")
        if self.max_state_bytes is not None and len(_json(vector).encode()) > self.max_state_bytes:
            raise CapacityError("The complete logical continuation exceeds its byte budget.")

    def _load(self, state):
        if state is None:
            return self._new_state()
        try:
            if type(state) is not dict or set(state) != set(self._new_state()):
                raise StateError("Unexpected state fields.")
            vector = json.loads(_json(state))
            if type(vector["v"]) is not int or vector["v"] != 1:
                raise StateError("Unsupported state version.")
            if (vector["plan"], vector["scope"]) != (self.plan.fingerprint, self.plan.scope):
                raise StateError("Continuation is bound to another plan or scope.")
            active = vector["stage"]
            if not _integer(active) or active >= len(self.plan.stages):
                raise StateError("Invalid active stage; terminal continuation is None.")
            for key in ("cursors", "selected", "buffers"):
                if type(vector[key]) is not list or len(vector[key]) != len(self.plan.stages):
                    raise StateError(f"Invalid {key} array.")
            for i, stage in enumerate(self.plan.stages):
                cursor = vector["cursors"][i]
                self._validate_position(cursor, i)
                count = vector["selected"][i]
                if not _integer(count) or count > len(stage.items) or (stage.quota is not None and count > stage.quota):
                    raise StateError("Invalid selected count.")
                if i > active and (cursor is not None or count):
                    raise StateError("An unstarted stage cannot have consumed progress.")
                buffer = vector["buffers"][i]
                if stage.buffer_size is None:
                    if buffer is not None:
                        raise StateError("A raw source cannot have prepared state.")
                    continue
                if type(buffer) is not dict or set(buffer) != {"items", "fetch", "exhausted"}:
                    raise StateError("Invalid prepared buffer.")
                self._validate_ids(buffer["items"], self.plan._members[i])
                self._validate_position(buffer["fetch"], i)
                if len(buffer["items"]) > stage.buffer_size or type(buffer["exhausted"]) is not bool:
                    raise StateError("Prepared state exceeds its declared shape or bound.")
                if i != active and buffer["items"]:
                    raise StateError("Only the active stage retains buffered work in this model.")
                positions = [self.plan._items[identity].position for identity in buffer["items"]]
                if positions != sorted(positions, reverse=True):
                    raise StateError("Prepared entries must remain in canonical order.")
                if positions and (buffer["fetch"] is None or positions[-1] != tuple(buffer["fetch"])):
                    raise StateError("The fetch cursor must identify the unread buffer tail.")
                if cursor is not None and any(position >= tuple(cursor) for position in positions):
                    raise StateError("Buffered entries must follow consumed progress.")
                if cursor is not None and buffer["fetch"] is not None and tuple(buffer["fetch"]) > tuple(cursor):
                    raise StateError("Fetch progress cannot precede consumed progress.")
            self._validate_ids(vector["coverage"], self.plan._items)
            self._validate_ids(vector["pending"], self.plan._claims)
            self._validate_ids(vector["omitted"], self.plan._claims)
            if set(vector["pending"]) & set(vector["omitted"]) or set(vector["coverage"]) & set(vector["omitted"]):
                raise StateError("Omitted, pending and selected coverage states conflict.")
            if any(self.plan._claims[identity].owner < active for identity in vector["pending"]):
                raise StateError("A completed owner cannot retain pending reservations.")
            if any(self.plan._claims[identity].owner >= active or self.plan._claims[identity].on_unserved != "omit" for identity in vector["omitted"]):
                raise StateError("Omissions must follow their owner's completion policy.")
            return vector
        except StateError:
            raise
        except (TypeError, ValueError, KeyError, OverflowError, RecursionError) as exc:
            raise StateError("State must be finite JSON with the declared shape.") from exc

    def _validate_position(self, position, index):
        if position is None:
            return
        if (type(position) is not list or len(position) != 2 or type(position[0]) is not int
                or type(position[1]) is not str or position[1] not in self.plan._members[index]
                or tuple(position) != self.plan._items[position[1]].position):
            raise StateError("A cursor must be a complete position from its source.")

    @staticmethod
    def _validate_ids(ids, allowed):
        if (type(ids) is not list or any(type(identity) is not str or identity not in allowed for identity in ids)
                or len(set(ids)) != len(ids)):
            raise StateError("State contains invalid or duplicate IDs.")

    def _reachable(self, identity, vector):
        position = self.plan._items[identity].position
        for index in range(vector["stage"], len(self.plan.stages)):
            if identity not in self.plan._members[index]:
                continue
            cursor = vector["cursors"][index]
            if cursor is not None and position >= tuple(cursor):
                continue
            quota = self.plan.stages[index].quota
            if quota is None or vector["selected"][index] < quota:
                return True
            # A zero-quota owner still has to distinguish covered from unserved
            # pending work when it completes. Other zero-quota sources need none.
            if identity in vector["pending"] and self.plan._claims[identity].owner == index:
                return True
        return False

    def _prune(self, vector):
        for field_name in ("coverage", "omitted"):
            vector[field_name] = sorted(identity for identity in vector[field_name] if self._reachable(identity, vector))

    def _finish(self, vector):
        """Resolve the owner while prior coverage still exists, then retire the stage."""
        index = vector["stage"]
        pending = []
        for identity in vector["pending"]:
            claim = self.plan._claims[identity]
            if claim.owner != index:
                pending.append(identity)
            elif identity not in vector["coverage"] and claim.on_unserved == "omit":
                vector["omitted"].append(identity)
        vector["pending"] = pending
        if vector["buffers"][index] is not None:
            vector["buffers"][index]["items"] = []
        vector["stage"] += 1
        self._prune(vector)

    def _settle_quotas(self, vector):
        while vector["stage"] < len(self.plan.stages):
            index = vector["stage"]
            quota = self.plan.stages[index].quota
            if quota is None or vector["selected"][index] < quota:
                break
            self._finish(vector)

    def _read(self, index, cursor, limit):
        stage = self.plan.stages[index]
        try:
            batch = self.source.read(stage, None if cursor is None else tuple(cursor), limit)
        except Exception as exc:
            raise ReadError("Source failed; retry the unchanged incoming continuation.") from exc
        if not isinstance(batch, Batch) or batch.complete is not True or type(batch.exhausted) is not bool:
            raise ReadError("Incomplete or malformed source response.")
        try:
            items = tuple(batch.items)
        except Exception as exc:
            raise ReadError("A source batch must contain an ordered item sequence.") from exc
        expected = tuple(item for item in stage.items if cursor is None or item.position < tuple(cursor))
        if len(items) > limit or items != expected[:len(items)]:
            raise ReadError("Source violated its ordered, complete-prefix contract.")
        if batch.exhausted and len(items) != len(expected):
            raise ReadError("Source reported false exhaustion.")
        if not items and not batch.exhausted:
            raise ReadError("Source made no progress and supplied no exhaustion proof.")
        return Batch(items, batch.exhausted)

    def page(self, state=None, *, page_size=50, batch_size=50, candidate_budget=1000):
        for value in (page_size, batch_size, candidate_budget):
            if not _integer(value, 1):
                raise ValueError("Page, batch and candidate limits must be positive integers.")
        vector = self._load(state)
        self._settle_quotas(vector)
        self._prune(vector)
        self._check_capacity(vector)
        rows = []
        consumed = fetched = reads = 0
        while len(rows) < page_size and consumed < candidate_budget and vector["stage"] < len(self.plan.stages):
            index = vector["stage"]
            stage = self.plan.stages[index]
            buffer = vector["buffers"][index]
            limit = min(batch_size, candidate_budget - consumed)
            if buffer is not None:
                if not buffer["items"]:
                    if buffer["exhausted"]:
                        self._finish(vector)
                        self._settle_quotas(vector)
                        continue
                    batch = self._read(index, buffer["fetch"], min(limit, stage.buffer_size))
                    reads += 1
                    fetched += len(batch.items)
                    buffer["items"] = [item.id for item in batch.items]
                    if batch.items:
                        buffer["fetch"] = list(batch.items[-1].position)
                    buffer["exhausted"] = batch.exhausted
                    self._check_capacity(vector)
                items = tuple(self.plan._items[identity] for identity in buffer["items"])
                exhausted = buffer["exhausted"]
            else:
                batch = self._read(index, vector["cursors"][index], limit)
                reads += 1
                fetched += len(batch.items)
                items, exhausted = batch.items, batch.exhausted
            processed = 0
            for item in items:
                if len(rows) == page_size or consumed == candidate_budget or vector["stage"] != index:
                    break
                claim = self.plan._claims.get(item.id)
                pending = item.id in vector["pending"]
                excluded = item.id in vector["coverage"] or item.id in vector["omitted"]
                reserved = pending and claim.owner != index and index in claim.yielding
                # Decide first; advancing the cursor alone does not make a duplicate safe.
                vector["cursors"][index] = list(item.position)
                consumed += 1
                processed += 1
                if buffer is not None:
                    assert buffer["items"].pop(0) == item.id
                if not excluded and not reserved:
                    rows.append((stage.name, item.id))
                    vector["selected"][index] += 1
                    vector["coverage"].append(item.id)
                if pending and claim.owner == index:
                    vector["pending"].remove(item.id)
                self._settle_quotas(vector)
                if vector["stage"] == index and buffer is not None and buffer["exhausted"] and not buffer["items"]:
                    self._finish(vector)
                    self._settle_quotas(vector)
                self._prune(vector)
                self._check_capacity(vector)
            if vector["stage"] == index and exhausted and processed == len(items):
                self._finish(vector)
                self._settle_quotas(vector)
                self._check_capacity(vector)
        self._settle_quotas(vector)
        self._prune(vector)
        self._check_capacity(vector)
        continuation = None if vector["stage"] == len(self.plan.stages) else vector
        return Page(tuple(rows), continuation, consumed, fetched, reads)
