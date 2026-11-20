"""Whole-result specification for grouped coverage and partial reservations.

This oracle keeps complete selected/omitted identity sets. It uses only public
plan records, source membership and original reservation declarations: no paging,
span codec, retained-state pruning or runtime grouping helper is shared with the
implementation under test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Plan


def unpaged_oracle(plan: Plan) -> list[tuple[str, str, tuple[str, ...]]]:
    """Return complete ordered rows as (stage, representative ID, member IDs).

    A grouped source candidate names a fixed (group, episode) unit in the shared
    record scope. Its row always subtracts prior coverage and deliberate omissions.
    ``subtract`` then removes members reserved for another owner where this stage
    yields. ``defer`` skips the entire candidate row if such members remain.

    The representative itself must be selectable. One emitted row consumes one
    stage slot regardless of member count. Skipping an owner's candidate because
    its grouped row is deferred does not resolve that unselected reservation.
    On owner completion, remaining unserved claims follow the declared policy.

    The caller supplies a valid immutable Plan. This direct traversal deliberately
    retains full history, so group expansion cannot reintroduce an earlier member
    merely because a candidate scan has passed its position.
    """
    records = {record.id: record for record in plan.records}
    stage_names = tuple(stage.name for stage in plan.stages)
    groups: dict[tuple[str, str], list[str]] = {}
    for record in sorted(plan.records, key=lambda value: (value.sort_key, value.id), reverse=True):
        groups.setdefault((record.group, record.episode), []).append(record.id)

    declarations = {reservation.owner: reservation for reservation in plan.reservations}
    owners: dict[str, tuple[str, frozenset[str], str]] = {}
    for owner in stage_names:
        reservation = declarations.get(owner)
        if reservation is None:
            continue
        yielding = frozenset(name for name in stage_names if name != owner) \
            if reservation.yield_from is None else frozenset(reservation.yield_from)
        for identity in reservation.item_ids:
            owners.setdefault(identity, (owner, yielding, reservation.on_unserved))

    selected: set[str] = set()
    omitted: set[str] = set()
    pending = set(owners)
    output: list[tuple[str, str, tuple[str, ...]]] = []
    for stage in plan.stages:
        candidates = sorted(stage.candidate_ids,
                            key=lambda identity: (records[identity].sort_key, identity), reverse=True)
        selected_rows = 0
        for candidate in candidates:
            if stage.quota is not None and selected_rows >= stage.quota:
                break
            claim = owners.get(candidate)
            if candidate in selected:
                if candidate in pending and claim[0] == stage.name:
                    pending.remove(candidate)
                continue
            if candidate in omitted:
                continue
            if candidate in pending and claim[0] != stage.name and stage.name in claim[1]:
                continue

            record = records[candidate]
            represented = (candidate,) if stage.mode == "items" else groups[(record.group, record.episode)]
            available = [identity for identity in represented
                         if identity not in selected and identity not in omitted]
            blocked = {identity for identity in available
                       if identity in pending
                       and owners[identity][0] != stage.name
                       and stage.name in owners[identity][1]}
            if stage.mode == "grouped" and stage.partial_policy == "defer" and blocked:
                continue
            members = tuple(identity for identity in available if identity not in blocked)
            if not members:
                continue
            output.append((stage.name, candidate, members))
            selected_rows += 1
            selected.update(members)
            pending.difference_update(identity for identity in members
                                      if identity in owners and owners[identity][0] == stage.name)

        finished = {identity for identity in pending if owners[identity][0] == stage.name}
        omitted.update(identity for identity in finished
                       if identity not in selected and owners[identity][2] == "omit")
        pending.difference_update(finished)
    return output
