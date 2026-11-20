"""Fixed-data grouped pagination, with IDs, fixed spans or byte-aware hybrid coverage.

Only metadata is selected. Group membership is a supplied (group, episode) unit;
discovering episodes, database execution and presentation hydration are out of
scope. Sources are raw complete prefixes, without persistent prepared buffers.
Continuation dictionaries are trusted logical state, not authenticated tokens.
"""

from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType

from reference.model import (
    Batch, CapacityError, Item, MemorySource, PlanError, ReadError, Reservation,
    Stage as RawStage, StateError,
)
from .coverage import CoverageError, encode_coverage, expand_coverage


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


@dataclass(frozen=True)
class Record:
    id: str
    sort_key: int
    group: str
    episode: str = "all"

    def __post_init__(self):
        if (any(type(value) is not str or not value for value in (self.id, self.group, self.episode))
                or type(self.sort_key) is not int):
            raise PlanError("A record needs string ID/group/episode and an integer sort key.")

    @property
    def position(self):
        return self.sort_key, self.id


@dataclass(frozen=True)
class Stage:
    name: str
    candidate_ids: tuple[str, ...]
    quota: int | None = None
    mode: str = "grouped"
    partial_policy: str = "subtract"

    def __post_init__(self):
        ids = tuple(self.candidate_ids)
        if type(self.name) is not str or not self.name:
            raise PlanError("Stages need nonempty names.")
        if any(type(identity) is not str or not identity for identity in ids) or len(set(ids)) != len(ids):
            raise PlanError("A source contains distinct, nonempty candidate IDs.")
        if self.quota is not None and not _integer(self.quota):
            raise PlanError("A quota is a nonnegative row count or None.")
        if self.mode not in ("items", "grouped") or self.partial_policy not in ("subtract", "defer"):
            raise PlanError("Unknown row mode or partial-reservation policy.")
        object.__setattr__(self, "candidate_ids", ids)


@dataclass(frozen=True)
class Plan:
    records: tuple[Record, ...]
    stages: tuple[Stage, ...]
    reservations: tuple[Reservation, ...] = ()
    scope: str = "default"
    fingerprint: str = field(init=False)
    _records: object = field(init=False, repr=False, compare=False)
    _units: object = field(init=False, repr=False, compare=False)
    _claims: object = field(init=False, repr=False, compare=False)
    _raw: tuple = field(init=False, repr=False, compare=False)
    _reach: tuple = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        records, stages, reservations = tuple(self.records), tuple(self.stages), tuple(self.reservations)
        if not all(isinstance(r, Record) for r in records) or not all(isinstance(s, Stage) for s in stages):
            raise PlanError("Plans require Record and Stage values.")
        if not all(isinstance(r, Reservation) for r in reservations):
            raise PlanError("Reservations require the core Reservation type.")
        if type(self.scope) is not str or not self.scope:
            raise PlanError("Plans need a nonempty fixed scope.")
        by_id = {r.id: r for r in records}
        names = {s.name: i for i, s in enumerate(stages)}
        if len(by_id) != len(records) or len(names) != len(stages):
            raise PlanError("Record IDs and stage names must be unique.")
        units = {}
        for record in sorted(records, key=lambda r: r.position, reverse=True):
            units.setdefault((record.group, record.episode), []).append(record.id)
        units = {key: tuple(value) for key, value in units.items()}
        raw, reach = [], []
        for stage in stages:
            if not set(stage.candidate_ids) <= by_id.keys():
                raise PlanError("Source contains an unknown scoped ID.")
            raw.append(RawStage(stage.name, tuple(Item(identity, by_id[identity].sort_key)
                                                 for identity in stage.candidate_ids), stage.quota))
            # The last raw candidate for a unit can still expand every unit member.
            # Precompute that dependency; raw member positions alone are insufficient.
            last = {}
            for item in raw[-1].items:
                record = by_id[item.id]
                key = (record.group, record.episode) if stage.mode == "grouped" else item.id
                last[key] = item.position
            reach.append(tuple((position, units[key] if stage.mode == "grouped" else (key,))
                               for key, position in last.items()))
        declared, claims = set(), {}
        for reservation in reservations:
            if reservation.owner not in names or reservation.owner in declared:
                raise PlanError("Reservation owners must be known and uniquely declared.")
            declared.add(reservation.owner)
            owner = names[reservation.owner]
            available = {identity for _, members in reach[owner] for identity in members}
            if not set(reservation.item_ids) <= available:
                raise PlanError("Reserved IDs must be representable by the owner's source.")
            if reservation.yield_from is not None and (
                    not set(reservation.yield_from) <= names.keys() or reservation.owner in reservation.yield_from):
                raise PlanError("Yielding stages must be known non-owner stages.")
        for reservation in sorted(reservations, key=lambda r: names[r.owner]):
            owner = names[reservation.owner]
            yielding = frozenset(names[name] for name in reservation.yield_from) \
                if reservation.yield_from is not None else frozenset(set(names.values()) - {owner})
            for identity in reservation.item_ids:
                claims.setdefault(identity, (owner, yielding, reservation.on_unserved))
        signature = {
            "scope": self.scope,
            "records": sorted((r.id, r.sort_key, r.group, r.episode) for r in records),
            "stages": [(s.name, [item.id for item in raw[i].items], s.quota, s.mode, s.partial_policy)
                       for i, s in enumerate(stages)],
            "claims": {identity: [owner, sorted(yielding), policy]
                       for identity, (owner, yielding, policy) in sorted(claims.items())},
        }
        for name, value in (("records", records), ("stages", stages), ("reservations", reservations),
                            ("_raw", tuple(raw)), ("_reach", tuple(reach))):
            object.__setattr__(self, name, value)
        for name, value in (("_records", by_id), ("_units", units), ("_claims", claims)):
            object.__setattr__(self, name, MappingProxyType(value))
        object.__setattr__(self, "fingerprint", hashlib.sha256(_json(signature).encode()).hexdigest())


@dataclass(frozen=True)
class Page:
    rows: tuple[tuple[str, str, tuple[str, ...]], ...]
    state: dict | None
    consumed: int
    fetched: int
    reads: int


class Pager:
    def __init__(self, plan, representation="ids", source=None, *,
                 max_state_entries=100000, max_state_bytes=10000000):
        if not isinstance(plan, Plan) or representation not in ("ids", "spans", "hybrid"):
            raise ValueError("Supply a grouped Plan and an IDs/spans/hybrid representation.")
        if not _integer(max_state_entries, 1) or not _integer(max_state_bytes, 1):
            raise ValueError("State limits must be positive integers.")
        self.plan, self.representation = plan, representation
        self.source = MemorySource() if source is None else source
        self.max_state_entries, self.max_state_bytes = max_state_entries, max_state_bytes
        self._decoded = {}
        self._group_records = {}
        for record in plan.records:
            self._group_records.setdefault(record.group, []).append(record)

    def _entry_members(self, entry):
        if self.representation == "ids" or (self.representation == "hybrid" and type(entry) is str):
            key = ("id", entry)
        else:
            key = tuple(entry) if entry[0] == "id" else (entry[0], entry[1], tuple(entry[2]), tuple(entry[3]))
        if key not in self._decoded:
            if key[0] == "id":
                members = (key[1],)
            else:
                _, group, lo, hi = key
                members = (r.id for r in self._group_records[group]
                           if lo <= r.position <= hi)
            self._decoded[key] = frozenset(members)
        return self._decoded[key]

    def _covered(self, state):
        return {identity for entry in state["coverage"] for identity in self._entry_members(entry)}

    def _encode_coverage(self, members):
        """Admission hook for local experiments using a different wire encoding."""
        return encode_coverage(self.plan.records, members, self.representation)

    def _needed(self, state):
        needed = set(state["pending"])
        for index in range(state["stage"], len(self.plan.stages)):
            stage = self.plan.stages[index]
            if stage.quota is not None and state["selected"][index] >= stage.quota:
                continue
            cursor = state["cursors"][index]
            for last, members in self.plan._reach[index]:
                if cursor is None or last < tuple(cursor):
                    needed.update(members)
        return needed

    def _prune(self, state):
        needed = self._needed(state)
        # Fixed spans retire whole; they never shrink merely on a first duplicate.
        state["coverage"] = [entry for entry in state["coverage"]
                             if self._entry_members(entry) & needed]
        # Deliberate omissions remain explicit and conservative until completion.

    def _load(self, state):
        n = len(self.plan.stages)
        initial = {"v": 1, "plan": self.plan.fingerprint, "scope": self.plan.scope,
                   "representation": self.representation, "stage": 0, "cursors": [None] * n,
                   "selected": [0] * n, "coverage": [], "pending": sorted(self.plan._claims), "omitted": []}
        if state is None:
            return initial
        try:
            vector = json.loads(_json(state))
            if type(vector) is not dict or set(vector) != set(initial):
                raise ValueError("Unknown state shape.")
            if any(vector[key] != initial[key] for key in ("v", "plan", "scope", "representation")):
                raise ValueError("State is bound to another plan or coverage mode.")
            if type(vector["v"]) is not int or not _integer(vector["stage"]) or vector["stage"] >= n:
                raise ValueError("Continuation needs an unfinished stage.")
            for key in ("cursors", "selected"):
                if type(vector[key]) is not list or len(vector[key]) != n:
                    raise ValueError("Invalid stage vector.")
            for i, stage in enumerate(self.plan.stages):
                count, cursor = vector["selected"][i], vector["cursors"][i]
                if not _integer(count) or count > len(stage.candidate_ids) or (stage.quota is not None and count > stage.quota):
                    raise ValueError("Invalid selected row count.")
                if cursor is not None and (type(cursor) is not list or len(cursor) != 2
                        or type(cursor[0]) is not int or type(cursor[1]) is not str
                        or cursor[1] not in stage.candidate_ids
                        or tuple(cursor) != self.plan._records[cursor[1]].position):
                    raise ValueError("Cursor is not a source position.")
                if i > vector["stage"] and (cursor is not None or count != 0):
                    raise ValueError("Future stage already consumed.")
            for key in ("pending", "omitted"):
                values = vector[key]
                if type(values) is not list or any(type(v) is not str for v in values) \
                        or len(set(values)) != len(values) or not set(values) <= self.plan._claims.keys():
                    raise ValueError("Invalid reservation state.")
            if any(self.plan._claims[v][0] < vector["stage"] for v in vector["pending"]):
                raise ValueError("Completed owner still pending.")
            if any(self.plan._claims[v][0] >= vector["stage"] or self.plan._claims[v][2] != "omit"
                   for v in vector["omitted"]):
                raise ValueError("Invalid completed omission.")
            covered = expand_coverage(self.plan.records, vector["coverage"], self.representation)
            if covered & set(vector["omitted"]):
                raise ValueError("An omitted ID cannot be selected coverage.")
            return vector
        except (ValueError, TypeError, KeyError, CoverageError) as exc:
            raise StateError("Invalid grouped continuation: " + str(exc)) from exc

    def _finish(self, state):
        index, covered = state["stage"], self._covered(state)
        finishing = {identity for identity in state["pending"] if self.plan._claims[identity][0] == index}
        state["omitted"] = sorted(set(state["omitted"]) | {
            identity for identity in finishing
            if identity not in covered and self.plan._claims[identity][2] == "omit"})
        state["pending"] = sorted(set(state["pending"]) - finishing)
        state["stage"] += 1

    def _settle_quotas(self, state):
        while state["stage"] < len(self.plan.stages):
            index = state["stage"]
            quota = self.plan.stages[index].quota
            if quota is None or state["selected"][index] < quota:
                return
            self._finish(state)

    def _read(self, index, cursor, limit):
        stage = self.plan._raw[index]
        try:
            batch = self.source.read(stage, None if cursor is None else tuple(cursor), limit)
        except Exception as exc:
            raise ReadError("Source failed; incoming continuation is unchanged.") from exc
        if not isinstance(batch, Batch) or batch.complete is not True or type(batch.exhausted) is not bool:
            raise ReadError("Incomplete or malformed source response.")
        try:
            items = tuple(batch.items)
        except TypeError as exc:
            raise ReadError("Malformed batch items.") from exc
        remaining = tuple(item for item in stage.items if cursor is None or item.position < tuple(cursor))
        if len(items) > limit or items != remaining[:len(items)]:
            raise ReadError("Source violated the ordered complete-prefix contract.")
        if batch.exhausted and len(items) != len(remaining):
            raise ReadError("False exhaustion.")
        if not items and not batch.exhausted:
            raise ReadError("Empty batch without exhaustion proof.")
        return Batch(items, batch.exhausted)

    def _check_capacity(self, state):
        entries = sum(len(state[key]) for key in ("coverage", "pending", "omitted"))
        if entries > self.max_state_entries or len(_json(state).encode()) > self.max_state_bytes:
            raise CapacityError("Exact continuation exceeds the response-state capacity; retry unchanged input.")

    def page(self, state=None, *, page_size=50, batch_size=50, candidate_budget=1000):
        if not all(_integer(v, 1) for v in (page_size, batch_size, candidate_budget)):
            raise ValueError("Page, batch and candidate limits must be positive integers.")
        vector = self._load(state)
        self._settle_quotas(vector)
        self._prune(vector)
        rows = []
        consumed = fetched = reads = 0
        while len(rows) < page_size and consumed < candidate_budget and vector["stage"] < len(self.plan.stages):
            index = vector["stage"]
            stage = self.plan.stages[index]
            batch = self._read(index, vector["cursors"][index], min(batch_size, candidate_budget - consumed))
            reads += 1
            fetched += len(batch.items)
            processed = 0
            for item in batch.items:
                if len(rows) == page_size or consumed == candidate_budget or vector["stage"] != index:
                    break
                covered = self._covered(vector)
                pending, omitted = set(vector["pending"]), set(vector["omitted"])

                def blocked(identity):
                    claim = self.plan._claims.get(identity)
                    return identity in pending and claim[0] != index and index in claim[1]

                members = ()
                if item.id in covered:
                    if item.id in pending and self.plan._claims[item.id][0] == index:
                        pending.remove(item.id)
                elif item.id not in omitted and not blocked(item.id):
                    record = self.plan._records[item.id]
                    represented = (item.id,) if stage.mode == "items" else self.plan._units[(record.group, record.episode)]
                    available = tuple(identity for identity in represented if identity not in covered and identity not in omitted)
                    reserved = {identity for identity in available if blocked(identity)}
                    if not (stage.mode == "grouped" and stage.partial_policy == "defer" and reserved):
                        members = tuple(identity for identity in available if identity not in reserved)
                # Decide before advancing/pruning: this candidate may depend on old coverage.
                vector["cursors"][index] = list(item.position)
                consumed += 1
                processed += 1
                if members:
                    rows.append((stage.name, item.id, members))
                    vector["selected"][index] += 1
                    vector["coverage"].extend(self._encode_coverage(members))
                    pending.difference_update(identity for identity in members
                                              if identity in self.plan._claims and self.plan._claims[identity][0] == index)
                # A deferred owner's raw candidate is NOT a served reservation.
                vector["pending"] = sorted(pending)
                self._settle_quotas(vector)
                self._prune(vector)
            if vector["stage"] == index and batch.exhausted and processed == len(batch.items):
                self._finish(vector)
                self._settle_quotas(vector)
                self._prune(vector)
        self._settle_quotas(vector)
        self._prune(vector)
        continuation = None if vector["stage"] == len(self.plan.stages) else vector
        # Only emitted continuation capacity is bounded here, not transient RAM.
        if continuation is not None:
            self._check_capacity(continuation)
        return Page(tuple(rows), continuation, consumed, fetched, reads)
