"""Exact explicit-ID and group-span representations over a fixed record scope.

An episode affects which members a row represents; it does not change the span
predicate. A span means ``group == key and lo <= (sort_key, id) <= hi`` over every
record in the shared eligible scope. The encoder therefore splits at every
uncovered member of that same group, including members of another episode or
members absent from a stage's candidate query. Other groups may interleave freely.

This small reference codec enumerates scoped membership. It demonstrates exact
set equivalence, not cheap group discovery or engine-query performance.
"""

import json


class CoverageError(ValueError):
    pass


def _mode(mode):
    if mode not in ("ids", "spans", "hybrid"):
        raise CoverageError("Coverage mode must be 'ids', 'spans' or 'hybrid'.")


def _json_bytes(entries):
    return len(json.dumps(entries, ensure_ascii=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8"))


def _scope(records):
    identities, groups = {}, {}
    for record in records:
        try:
            identity, key, group = record.id, record.sort_key, record.group
        except AttributeError as exc:
            raise CoverageError("Scope records need id, sort_key and group metadata.") from exc
        if type(identity) is not str or not identity or type(key) is not int:
            raise CoverageError("Scope IDs must be nonempty strings and sort keys integers.")
        if type(group) is not str or not group:
            raise CoverageError("Scope group keys must be nonempty strings.")
        if identity in identities:
            raise CoverageError("Scope IDs must be unique.")
        position = (key, identity)
        identities[identity] = (group, position)
        groups.setdefault(group, []).append((position, identity))
    for members in groups.values():
        members.sort()
    return identities, groups


def encode_coverage(records, covered_ids, mode):
    """Return a deterministic representation of exactly the supplied ID set.

    ``covered_ids`` has set semantics: repeating an input ID does not add coverage.
    Span mode uses one span per maximal selected run within a group's full ordered
    scope, or a tagged ID for a singleton. Hybrid mode uses bare string IDs unless
    a span strictly reduces the compact, ASCII-escaped JSON byte count for that
    entire run. It includes separators in that comparison and keeps IDs on a tie.
    This is a per-maximal-run choice, not a globally optimal partition of a run.
    """
    _mode(mode)
    identities, groups = _scope(records)
    if isinstance(covered_ids, (str, bytes)):
        raise CoverageError("Coverage must be an iterable of IDs, not one string.")
    try:
        supplied = tuple(covered_ids)
    except TypeError as exc:
        raise CoverageError("Coverage must be an iterable of IDs.") from exc
    if any(type(identity) is not str or identity not in identities for identity in supplied):
        raise CoverageError("Coverage contains an unknown or invalid ID.")
    selected = set(supplied)
    if mode == "ids":
        return sorted(selected)

    entries = []

    def emit(group, run):
        if len(run) == 1:
            entries.append(run[0][1] if mode == "hybrid" else ["id", run[0][1]])
        elif run:
            span = ["span", group, list(run[0][0]), list(run[-1][0])]
            explicit = [identity for _, identity in run]
            if mode == "hybrid" and _json_bytes([span]) >= _json_bytes(explicit):
                entries.extend(explicit)
            else:
                entries.append(span)

    for group, members in sorted(groups.items()):
        run = []
        for position, identity in members:
            if identity in selected:
                run.append((position, identity))
            else:
                emit(group, run)
                run = []
        emit(group, run)
    return entries


def _identity(identity, identities):
    if type(identity) is not str or identity not in identities:
        raise CoverageError("Coverage contains an unknown or invalid ID.")
    return identity


def _position(value, group, identities):
    if (type(value) is not list or len(value) != 2
            or type(value[0]) is not int or type(value[1]) is not str):
        raise CoverageError("A span bound must be a full [integer sort_key, string id] position.")
    identity = _identity(value[1], identities)
    position = tuple(value)
    if identities[identity] != (group, position):
        raise CoverageError("A span bound must identify a scoped member of its group.")
    return position


def expand_coverage(records, entries, mode):
    """Evaluate descriptor membership over the whole shared scope.

    Reject malformed descriptors, nonexistent bounds, duplicate explicit members
    and overlapping descriptor coverage. Non-overlapping adjacent spans are valid
    input, though the encoder would coalesce them into a canonical maximal run.
    Hybrid mode accepts bare string IDs and tagged spans; tagged singleton IDs
    belong only to span mode. Decoding does not enforce the encoder's cost choice.
    Decoding cannot establish that claimed members were actually selected; the
    independent pager/oracle comparison establishes that semantic requirement.
    """
    _mode(mode)
    identities, groups = _scope(records)
    if type(entries) is not list:
        raise CoverageError("Coverage representation must be a JSON list.")
    result = set()
    for entry in entries:
        if mode == "ids" or (mode == "hybrid" and type(entry) is str):
            members = {_identity(entry, identities)}
        else:
            if type(entry) is not list or not entry:
                raise CoverageError("A span-mode entry must be a tagged JSON list.")
            if mode == "spans" and entry[0] == "id" and len(entry) == 2:
                members = {_identity(entry[1], identities)}
            elif entry[0] == "span" and len(entry) == 4:
                group = entry[1]
                if type(group) is not str or group not in groups:
                    raise CoverageError("A span needs a group from the shared scope.")
                lo = _position(entry[2], group, identities)
                hi = _position(entry[3], group, identities)
                if lo > hi:
                    raise CoverageError("A span's lower bound cannot exceed its upper bound.")
                members = {identity for position, identity in groups[group] if lo <= position <= hi}
                if len(members) < 2:
                    raise CoverageError("A singleton requires an ID entry, not a span.")
            else:
                raise CoverageError("Unknown coverage tag or malformed entry length.")
        if result & members:
            raise CoverageError("Coverage entries contain duplicate or overlapping members.")
        result.update(members)
    return result


def contains(records, entries, identity, mode):
    """Convenience membership check; this reference implementation expands sets."""
    return identity in expand_coverage(records, entries, mode)
