"""Independent checks of returned core/reservation state at a page boundary.

This auditor uses public source rows, original reservation declarations and the
actual output history. It does not call pager helpers or inspect compiled plan
membership/claim maps. It checks the model's conservative retention contract:
an unread suffix of any remaining consumer can still need coverage. That is not
a claim that no smaller representation or stronger query proof could exist.

A single boundary cannot establish cursor monotonicity across requests; the
traversal harness checks that separately. This function checks that the boundary
agrees with its output history, source prefixes, ownership and unread buffers.
"""

from collections import Counter

from .oracle import unpaged_oracle


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _same_ids(field, actual, expected):
    actual_set = set(actual)
    _require(len(actual) == len(actual_set), f"{field}: duplicate retained IDs")
    missing, stale = expected - actual_set, actual_set - expected
    _require(not missing and not stale,
             f"{field}: missing={sorted(missing)[:8]} ({len(missing)}); "
             f"stale={sorted(stale)[:8]} ({len(stale)})")


def audit_boundary(plan, state, rows_so_far):
    """Assert exact retained sets and coherent progress; return None on success."""
    rows = list(rows_so_far)
    stage_index = {stage.name: index for index, stage in enumerate(plan.stages)}
    positions = {item.id: (item.sort_key, item.id)
                 for stage in plan.stages for item in stage.items}
    members = [{item.id for item in stage.items} for stage in plan.stages]
    ordered = [sorted(stage.items, key=lambda item: (item.sort_key, item.id), reverse=True)
               for stage in plan.stages]
    chosen_at = {}
    counts = Counter()
    previous_index, previous_position = -1, None
    for stage_name, identity in rows:
        _require(stage_name in stage_index, f"history: unknown stage {stage_name}")
        index = stage_index[stage_name]
        _require(identity in members[index], f"history: {identity} is not in {stage_name}")
        _require(identity not in chosen_at, f"history: repeated ID {identity}")
        _require(index >= previous_index, "history: stage order moved backwards")
        position = positions[identity]
        if index == previous_index:
            _require(position < previous_position, f"history: order moved backwards in {stage_name}")
        previous_index, previous_position = index, position
        chosen_at[identity] = index
        counts[index] += 1

    if state is None:
        _require(rows == unpaged_oracle(plan), "terminal history differs from the unpaged oracle")
        return

    active = state["stage"]
    _require(0 <= active < len(plan.stages), "stage: invalid active stage")
    _require(previous_index <= active, "history: output from an unstarted stage")
    expected_counts = [counts[index] for index in range(len(plan.stages))]
    _require(state["selected"] == expected_counts, "selected counts disagree with output history")

    # Build suffixes once per consumer, not by invoking a per-ID pager predicate.
    suffixes = []
    selectable = set()
    for index, stage in enumerate(plan.stages):
        cursor = state["cursors"][index]
        cursor = None if cursor is None else tuple(cursor)
        stage_positions = {(item.sort_key, item.id) for item in ordered[index]}
        _require(cursor is None or cursor in stage_positions, f"cursor: invalid source position for {stage.name}")
        suffix = [item.id for item in ordered[index]
                  if cursor is None or (item.sort_key, item.id) < cursor]
        suffixes.append(suffix)
        _require(stage.quota is None or counts[index] <= stage.quota,
                 f"quota exceeded in {stage.name}")
        selected_here = {identity for identity, owner in chosen_at.items() if owner == index}
        _require(not selected_here or cursor is not None,
                 f"cursor: {stage.name} selected rows without consumed progress")
        if cursor is not None:
            _require(all(positions[identity] >= cursor for identity in selected_here),
                     f"cursor: {stage.name} has not consumed one of its selected rows")
        if index > active:
            _require(cursor is None and not counts[index], f"unstarted stage {stage.name} has progress")
        if index < active:
            quota_met = stage.quota is not None and counts[index] == stage.quota
            _require(quota_met or not suffix, f"completed stage {stage.name} has unfinished work")
        if index == active:
            _require(stage.quota is None or counts[index] < stage.quota,
                     f"active stage {stage.name} has already reached its quota")
        if index >= active and (stage.quota is None or counts[index] < stage.quota):
            selectable.update(suffix)

        buffer = state["buffers"][index]
        if stage.buffer_size is None:
            _require(buffer is None, f"buffer: raw source {stage.name} retained prepared work")
            continue
        _require(buffer is not None, f"buffer: missing state for prepared source {stage.name}")
        _require(len(buffer["items"]) <= stage.buffer_size, f"buffer: {stage.name} exceeds its bound")
        fetch = None if buffer["fetch"] is None else tuple(buffer["fetch"])
        _require(fetch is None or fetch in stage_positions, f"buffer: invalid fetch position for {stage.name}")
        if index != active:
            _require(not buffer["items"], f"buffer: inactive stage {stage.name} retains unread work")
            if index > active:
                _require(fetch is None and not buffer["exhausted"],
                         f"buffer: unstarted stage {stage.name} has fetch progress")
            continue
        # All rows between consumed and fetched progress remain unread. A fetch
        # cursor beyond a duplicate is therefore not a retirement proof.
        unread = [] if fetch is None else [identity for identity in suffix if positions[identity] >= fetch]
        _require(buffer["items"] == unread, f"buffer: unread source interval differs for {stage.name}")
        if cursor is not None:
            _require(fetch is not None and fetch <= cursor,
                     f"buffer: fetch precedes consumed progress for {stage.name}")
        if buffer["exhausted"]:
            _require(buffer["items"] == suffix, f"buffer: false exhaustion in {stage.name}")

    declarations = {reservation.owner: reservation for reservation in plan.reservations}
    winners = {}
    for index, stage in enumerate(plan.stages):
        declaration = declarations.get(stage.name)
        if declaration is not None:
            for identity in declaration.item_ids:
                winners.setdefault(identity, (index, declaration.on_unserved))

    unread_by_owner = [set(suffix) for suffix in suffixes]
    pending = {identity for identity, (owner, _) in winners.items()
               if owner >= active and identity in unread_by_owner[owner]}
    _same_ids("pending", state["pending"], pending)

    # A pending zero-quota owner must still distinguish already-covered from
    # unserved work when it resolves its reservation. It is a state consumer even
    # though it cannot select another output row.
    ownership_dependencies = {identity for identity in pending
                              if plan.stages[winners[identity][0]].quota == 0}
    needed = selectable | ownership_dependencies
    _same_ids("coverage", state["coverage"], set(chosen_at) & needed)

    omitted = {identity for identity, (owner, policy) in winners.items()
               if owner < active and policy == "omit"
               and (identity not in chosen_at or chosen_at[identity] > owner)}
    _same_ids("omitted", state["omitted"], omitted & needed)
