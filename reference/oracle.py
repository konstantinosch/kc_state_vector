"""Whole-result specification for valid fixed-data core/reservation plans.

This oracle has no cursors, pages, batches, retained exclusions, or pruning.
It deliberately repeats the small ownership rule rather than sharing engine
implementation, so agreement is meaningful evidence for the paged model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Plan


def unpaged_oracle(plan: Plan) -> list[tuple[str, str]]:
    """Build the ordered result directly from source sets and stage quotas.

    An overlapping reservation belongs to the first claiming owner in plan
    order, independently of declaration order. A stage outside the winner's
    yielding set can select that ID before its owner. At owner completion an
    unserved winner is either omitted everywhere thereafter or released to
    remaining stages. A losing ownership claim never becomes active later.

    The caller supplies a valid Plan. Validation is intentionally not delegated
    to Pager: this module imports no runtime implementation code.
    """
    stage_names = tuple(stage.name for stage in plan.stages)
    declarations = {reservation.owner: reservation for reservation in plan.reservations}
    owners: dict[str, tuple[str, frozenset[str], str]] = {}
    for owner in stage_names:
        reservation = declarations.get(owner)
        if reservation is None:
            continue
        yielding = frozenset(
            name for name in stage_names if name != owner
        ) if reservation.yield_from is None else frozenset(reservation.yield_from)
        for item_id in reservation.item_ids:
            if item_id not in owners:
                owners[item_id] = (owner, yielding, reservation.on_unserved)

    selected: set[str] = set()
    omitted: set[str] = set()
    pending = set(owners)
    output: list[tuple[str, str]] = []
    for stage in plan.stages:
        eligible = [
            item.id
            for item in sorted(stage.items, key=lambda item: (item.sort_key, item.id), reverse=True)
            if item.id not in selected
            and item.id not in omitted
            and not (
                item.id in pending
                and owners[item.id][0] != stage.name
                and stage.name in owners[item.id][1]
            )
        ]
        chosen = eligible if stage.quota is None else eligible[:stage.quota]
        selected.update(chosen)
        output.extend((stage.name, item_id) for item_id in chosen)
        finished = {item_id for item_id in pending if owners[item_id][0] == stage.name}
        omitted.update(
            item_id for item_id in finished
            if item_id not in selected and owners[item_id][2] == "omit"
        )
        pending.difference_update(finished)
    return output
