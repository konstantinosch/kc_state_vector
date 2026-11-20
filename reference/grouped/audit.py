"""Independent boundary checks and paired ID/span/hybrid traversals.

The checker uses public records, source membership, original reservations and
complete output history. It does not use pager membership/pruning helpers or the
coverage codec. Grouped consumers can reach members above their raw cursor, so
their remaining candidates contribute whole fixed group/episode units to the
retention proof. Pending owners are consumers too, including deferred owners.

Retirement metrics describe descriptors visible at consecutive page boundaries.
Descriptors created and retired within a single request are deliberately not
counted. These are logical correctness/size measurements, not engine timings.
"""

from collections import Counter
import json

from .oracle import unpaged_oracle


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _same_ids(label, actual, expected, known):
    _require(type(actual) is list, f"{label}: expected a JSON list")
    _require(all(type(identity) is str and identity in known for identity in actual),
             f"{label}: unknown or invalid ID")
    supplied = set(actual)
    _require(len(supplied) == len(actual), f"{label}: duplicate IDs")
    missing, stale = expected - supplied, supplied - expected
    _require(not missing and not stale,
             f"{label}: missing={sorted(missing)[:8]} ({len(missing)}); "
             f"stale={sorted(stale)[:8]} ({len(stale)})")


def _decode(records, entries, representation):
    """Evaluate descriptor predicates directly, independently of coverage.py."""
    _require(type(entries) is list, "coverage: expected a JSON list")
    _require(representation in ("ids", "spans", "hybrid"), "coverage: unknown representation")
    groups = {}
    for identity, record in records.items():
        groups.setdefault(record.group, []).append(((record.sort_key, identity), identity))
    expanded, pieces = set(), []

    def identity(value):
        _require(type(value) is str and value in records, "coverage: invalid ID")
        return value

    def bound(value, group):
        _require(type(value) is list and len(value) == 2
                 and type(value[0]) is int and type(value[1]) is str,
                 "coverage: a bound needs the full [sort_key, id] position")
        member = records[identity(value[1])]
        _require(member.group == group and member.sort_key == value[0],
                 "coverage: a bound must name a scoped member of its group")
        return tuple(value)

    for entry in entries:
        if representation == "ids" or (representation == "hybrid" and type(entry) is str):
            members = {identity(entry)}
        else:
            _require(type(entry) is list and entry, "coverage: invalid tagged entry")
            if representation == "spans" and entry[0] == "id" and len(entry) == 2:
                members = {identity(entry[1])}
            elif entry[0] == "span" and len(entry) == 4:
                group = entry[1]
                _require(type(group) is str and group in groups, "coverage: invalid span group")
                lo, hi = bound(entry[2], group), bound(entry[3], group)
                _require(lo <= hi, "coverage: reversed span bounds")
                # The descriptor has no episode restriction. In particular, a
                # reserved or different-episode hole cannot be bridged silently.
                members = {member for position, member in groups[group] if lo <= position <= hi}
                _require(len(members) >= 2, "coverage: a singleton needs an ID entry")
            else:
                raise AssertionError("coverage: unknown tag or malformed entry")
        _require(not expanded.intersection(members), "coverage: overlapping descriptors")
        expanded.update(members)
        pieces.append(members)
    return expanded, pieces


def _audit_boundary(plan, state, rows_so_far, *, expected=None):
    """Return public counts plus the independently derived dependency set."""
    records = {record.id: record for record in plan.records}
    positions = {identity: (record.sort_key, identity) for identity, record in records.items()}
    names = {stage.name: index for index, stage in enumerate(plan.stages)}
    sources = [set(stage.candidate_ids) for stage in plan.stages]
    units = {}
    for identity, record in records.items():
        units.setdefault((record.group, record.episode), set()).add(identity)

    rows, chosen_at, representatives = [], {}, [[] for _ in plan.stages]
    counts = Counter()
    previous_stage, previous_position = -1, None
    for row in rows_so_far:
        _require(type(row) in (tuple, list) and len(row) == 3, "history: malformed row")
        stage_name, representative, members = row
        _require(type(stage_name) is str and stage_name in names, "history: unknown stage")
        index = names[stage_name]
        stage = plan.stages[index]
        _require(type(representative) is str and representative in sources[index],
                 f"history: representative is absent from {stage_name}'s source")
        _require(type(members) in (tuple, list) and members, "history: empty or malformed members")
        _require(all(type(member) is str and member in records for member in members),
                 "history: unknown member")
        members = tuple(members)
        _require(len(set(members)) == len(members), "history: duplicate members in one row")
        _require(representative in members, "history: representative must be newly represented")
        _require(members == tuple(sorted(members, key=positions.__getitem__, reverse=True)),
                 "history: members are not in descending full-position order")
        if stage.mode == "items":
            _require(members == (representative,), "history: item row represents other members")
        else:
            anchor = records[representative]
            _require(set(members) <= units[(anchor.group, anchor.episode)],
                     "history: row crosses its fixed group/episode unit")
        _require(not set(members).intersection(chosen_at), "history: a member was represented twice")
        _require(index >= previous_stage, "history: stage order moved backwards")
        position = positions[representative]
        if index == previous_stage:
            _require(position < previous_position, "history: representative order moved backwards")
        previous_stage, previous_position = index, position
        chosen_at.update((member, index) for member in members)
        representatives[index].append(representative)
        counts[index] += 1
        rows.append((stage_name, representative, members))

    for index, stage in enumerate(plan.stages):
        _require(stage.quota is None or counts[index] <= stage.quota,
                 f"history: quota exceeded in {stage.name}")
    oracle = unpaged_oracle(plan) if expected is None else list(expected)
    _require(len(rows) <= len(oracle) and rows == oracle[:len(rows)],
             "history: output is not a prefix of the independent oracle")
    summary = {"selected_rows": len(rows), "selected_members": len(chosen_at),
               "needed_members": 0, "live_selected_members": 0,
               "coverage_members": 0, "coverage_entries": 0,
               "coverage_bytes": 0, "span_entries": 0, "id_entries": 0,
               "pending_members": 0, "omitted_members": 0}
    if state is None:
        _require(rows == oracle, "terminal history differs from the independent oracle")
        return summary, set()

    _require(type(state) is dict, "state: expected an object or terminal None")
    required = {"v", "plan", "scope", "representation", "stage", "cursors",
                "selected", "coverage", "pending", "omitted"}
    _require(required <= state.keys(), "state: missing continuation fields")
    _require(type(state["v"]) is int and state["v"] > 0, "state: invalid version")
    _require(type(state["plan"]) is str and state["plan"], "state: invalid plan identity")
    _require(state["scope"] == plan.scope, "state: wrong record scope")
    active = state["stage"]
    _require(type(active) is int and 0 <= active < len(plan.stages), "stage: invalid active index")
    _require(previous_stage <= active, "history: output from an unstarted stage")
    _require(type(state["selected"]) is list
             and all(type(value) is int and value >= 0 for value in state["selected"])
             and state["selected"] == [counts[index] for index in range(len(plan.stages))],
             "selected: row counts disagree with history")
    _require(type(state["cursors"]) is list and len(state["cursors"]) == len(plan.stages),
             "cursors: invalid stage count")

    cursors, needed = [], set()
    for index, stage in enumerate(plan.stages):
        raw_cursor = state["cursors"][index]
        if raw_cursor is not None:
            _require(type(raw_cursor) is list and len(raw_cursor) == 2
                     and type(raw_cursor[0]) is int and type(raw_cursor[1]) is str,
                     f"cursor: invalid full position in {stage.name}")
            _require(raw_cursor[1] in sources[index]
                     and positions[raw_cursor[1]] == tuple(raw_cursor),
                     f"cursor: position is absent from {stage.name}'s source")
        cursor = None if raw_cursor is None else tuple(raw_cursor)
        cursors.append(cursor)
        suffix = {identity for identity in sources[index]
                  if cursor is None or positions[identity] < cursor}
        _require(not representatives[index] or cursor is not None,
                 f"cursor: {stage.name} emitted rows without consuming a candidate")
        if cursor is not None:
            _require(all(positions[identity] >= cursor for identity in representatives[index]),
                     f"cursor: {stage.name} has not consumed an emitted representative")
        if index > active:
            _require(cursor is None and not counts[index], f"stage: unstarted {stage.name} has progress")
        quota_met = stage.quota is not None and counts[index] == stage.quota
        if index < active:
            _require(quota_met or not suffix, f"stage: completed {stage.name} has unfinished work")
        if index == active:
            _require(not quota_met, f"stage: active {stage.name} already reached its quota")
        if index >= active and not quota_met:
            if stage.mode == "items":
                needed.update(suffix)
            else:
                reachable_units = {(records[identity].group, records[identity].episode)
                                   for identity in suffix}
                for unit in reachable_units:
                    needed.update(units[unit])

    # Winners follow stage precedence, not declaration order. Losing claims do
    # not return after the winning owner completes.
    declarations = {reservation.owner: reservation for reservation in plan.reservations}
    winners = {}
    for index, stage in enumerate(plan.stages):
        declaration = declarations.get(stage.name)
        if declaration is not None:
            for identity in declaration.item_ids:
                winners.setdefault(identity, (index, declaration.on_unserved))

    pending = set()
    for identity, (owner, _) in winners.items():
        if owner < active:
            continue
        if owner > active:
            pending.add(identity)
            continue
        selected_by = chosen_at.get(identity)
        if selected_by == owner:
            continue  # Expansion resolves own covered members before their scan.
        consumed = (identity in sources[owner] and cursors[owner] is not None
                    and positions[identity] >= cursors[owner])
        if selected_by is not None and consumed:
            continue  # Owner has encountered an already represented candidate.
        # An unselected candidate may have been consumed by a deferred grouped
        # row. Its claim still needs owner-completion release/omit processing.
        pending.add(identity)
    _same_ids("pending", state["pending"], pending, records)
    needed.update(pending)

    omitted = {identity for identity, (owner, policy) in winners.items()
               if owner < active and policy == "omit" and identity not in chosen_at}
    _same_ids("omitted", state["omitted"], omitted, records)
    represented = set(chosen_at)
    decoded, pieces = _decode(records, state["coverage"], state["representation"])
    _require(decoded <= represented, "coverage: a descriptor covers an unselected member")
    _require(decoded.intersection(needed) == represented.intersection(needed),
             "coverage: missing selected members required by future consumers")
    _require(all(piece.intersection(needed) for piece in pieces),
             "coverage: a retained descriptor has no remaining consumer")
    if state["representation"] == "ids":
        _require(decoded == represented.intersection(needed), "coverage: stale explicit IDs")
    span_entries = sum(type(entry) is list and entry[0] == "span" for entry in state["coverage"])
    summary.update(needed_members=len(needed), live_selected_members=len(represented & needed),
                   coverage_members=len(decoded), coverage_entries=len(pieces),
                   coverage_bytes=len(_json(state["coverage"]).encode("utf-8")),
                   span_entries=span_entries, id_entries=len(pieces) - span_entries,
                   pending_members=len(pending), omitted_members=len(omitted))
    return summary, needed


def audit_boundary(plan, state, rows_so_far, *, expected=None):
    """Check one continuation against complete history; return JSON-safe counts.

    ``expected`` can supply an already computed unpaged oracle result when many
    boundaries of the same plan are checked. Without it, the oracle is computed
    here. A terminal ``None`` continuation requires the complete oracle result.

    Exact IDs retire individually. A fixed span may retain previously selected
    members that are no longer needed while at least one member remains needed.
    Both representations must cover exactly the selected IDs on the needed set.
    Descriptor identity across boundaries is checked by ``run_comparison``.
    """
    summary, _ = _audit_boundary(plan, state, rows_so_far, expected=expected)
    return summary


def _page_signature(page):
    return _json({"rows": page.rows, "state": page.state, "consumed": page.consumed,
                  "fetched": page.fetched, "reads": page.reads})


def run_comparison(plan, page_size=50, batch_size=50, candidate_budget=1000, replay=True,
                   *, representations=("ids", "spans")):
    """Traverse selected representations, checking every prefix and continuation.

    State is JSON-round-tripped between requests and a fresh pager is used for
    every call. With ``replay=True``, the same input is also retried and its full
    result compared. Still-needed descriptors must survive unchanged across
    returned boundaries, even when a smaller replacement would remain exact.
    Returned metrics contain only JSON-safe values.
    """
    from .model import Pager

    for name, value in (("page_size", page_size), ("batch_size", batch_size),
                        ("candidate_budget", candidate_budget)):
        _require(type(value) is int and value > 0, f"{name}: expected a positive integer")
    _require(type(representations) in (tuple, list) and representations,
             "representations: expected a nonempty sequence of modes")
    _require(all(type(mode) is str and mode in ("ids", "spans", "hybrid") for mode in representations),
             "representations: unknown coverage mode")
    _require(len(set(representations)) == len(representations), "representations: duplicate mode")
    oracle = unpaged_oracle(plan)
    records = {record.id: record for record in plan.records}
    states = {mode: None for mode in representations}
    previous_entries = {mode: set() for mode in states}
    history, page_metrics = [], []
    metrics = {mode: {"peak_coverage_entries": 0, "peak_coverage_entries_page": 0,
                      "peak_coverage_entries_rows": 0,
                      "peak_coverage_members": 0, "peak_live_selected_members": 0,
                      "peak_span_entries": 0, "peak_id_entries": 0,
                      "peak_coverage_bytes": 0, "peak_coverage_bytes_page": 0,
                      "peak_coverage_bytes_rows": 0, "peak_coverage_bytes_full_pages": 0,
                      "peak_state_bytes": 0, "peak_state_bytes_page": 0,
                      "peak_state_bytes_rows": 0, "peak_state_bytes_full_pages": 0,
                      "retired_entries": 0, "retirement_pages": [], "retirements": []}
               for mode in states}
    requests = full_pages = empty_pages = consumed_total = fetched_total = reads_total = 0
    # With no prepared buffers, every nonterminal request consumes a candidate
    # or completes a stage. This intentionally generous bound catches cycles.
    request_limit = sum(len(stage.candidate_ids) for stage in plan.stages) + len(plan.stages) + 1
    while True:
        requests += 1
        _require(requests <= request_limit, "traversal: continuation failed to terminate")
        results = {}
        for mode in states:
            incoming = states[mode]
            before = _json(incoming)
            page = Pager(plan, representation=mode).page(
                state=incoming, page_size=page_size, batch_size=batch_size,
                candidate_budget=candidate_budget)
            _require(_json(incoming) == before, f"{mode}: page mutated its supplied continuation")
            _require(type(page.rows) in (tuple, list) and len(page.rows) <= page_size,
                     f"{mode}: page exceeded its row limit")
            for label in ("consumed", "fetched", "reads"):
                value = getattr(page, label)
                _require(type(value) is int and value >= 0, f"{mode}: invalid {label} count")
            _require(len(page.rows) <= page.consumed <= candidate_budget,
                     f"{mode}: emitted/consumed counts exceed candidate work")
            _require(page.consumed <= page.fetched <= page.reads * batch_size,
                     f"{mode}: read/fetch counts exceed bounded raw reads")
            if replay:
                retry_input = json.loads(before)
                retried = Pager(plan, representation=mode).page(
                    state=retry_input, page_size=page_size, batch_size=batch_size,
                    candidate_budget=candidate_budget)
                _require(_json(retry_input) == before, f"{mode}: replay mutated its input")
                _require(_page_signature(page) == _page_signature(retried),
                         f"{mode}: retry changed rows, work or continuation")
            if page.state is not None:
                _require(_json(page.state) != before, f"{mode}: nonterminal request made no progress")
                if incoming is not None:
                    _require(page.state["stage"] >= incoming["stage"], f"{mode}: stage moved backwards")
                    for old, new in zip(incoming["cursors"], page.state["cursors"]):
                        _require(old is None or (new is not None and tuple(new) <= tuple(old)),
                                 f"{mode}: consumed cursor moved backwards")
            results[mode] = page

        left = results[representations[0]]
        for mode, right in results.items():
            _require(left.rows == right.rows, f"paired traversal ({mode}): output rows differ")
            _require((left.consumed, left.fetched, left.reads) ==
                     (right.consumed, right.fetched, right.reads),
                     f"paired traversal ({mode}): candidate work differs")
            _require((left.state is None) == (right.state is None),
                     f"paired traversal ({mode}): termination differs")
            if left.state is not None:
                shared_left = {key: value for key, value in left.state.items()
                               if key not in ("representation", "coverage")}
                shared_right = {key: value for key, value in right.state.items()
                                if key not in ("representation", "coverage")}
                _require(shared_left == shared_right,
                         f"paired traversal ({mode}): shared continuation fields differ")
        rows_before = len(history)
        history.extend(left.rows)
        full_pages += len(left.rows) == page_size
        empty_pages += not left.rows
        consumed_total += left.consumed
        fetched_total += left.fetched
        reads_total += left.reads
        detail = {"page": requests, "rows": len(left.rows),
                  "rows_before": rows_before, "rows_so_far": len(history),
                  "full_pages_so_far": full_pages,
                  "represented_members": sum(len(row[2]) for row in left.rows),
                  "consumed": left.consumed, "fetched": left.fetched, "reads": left.reads,
                  "next_stage": None if left.state is None else left.state["stage"]}
        for mode, page in results.items():
            boundary, needed = _audit_boundary(plan, page.state, history, expected=oracle)
            state_bytes = 0 if page.state is None else len(_json(page.state).encode("utf-8"))
            entries = set() if page.state is None else {_json(entry) for entry in page.state["coverage"]}
            incoming = states[mode]
            if incoming is not None:
                _, prior_members = _decode(records, incoming["coverage"], mode)
                for entry, members in zip(incoming["coverage"], prior_members):
                    if members.intersection(needed):
                        _require(_json(entry) in entries,
                                 f"{mode}: a still-needed fixed descriptor changed or disappeared")
            retired = len(previous_entries[mode] - entries)
            previous_entries[mode] = entries
            mode_metrics = metrics[mode]
            if boundary["coverage_entries"] > mode_metrics["peak_coverage_entries"]:
                mode_metrics["peak_coverage_entries"] = boundary["coverage_entries"]
                mode_metrics["peak_coverage_entries_page"] = requests
                mode_metrics["peak_coverage_entries_rows"] = len(history)
            if boundary["coverage_bytes"] > mode_metrics["peak_coverage_bytes"]:
                mode_metrics["peak_coverage_bytes"] = boundary["coverage_bytes"]
                mode_metrics["peak_coverage_bytes_page"] = requests
                mode_metrics["peak_coverage_bytes_rows"] = len(history)
                mode_metrics["peak_coverage_bytes_full_pages"] = full_pages
            if state_bytes > mode_metrics["peak_state_bytes"]:
                mode_metrics["peak_state_bytes"] = state_bytes
                mode_metrics["peak_state_bytes_page"] = requests
                mode_metrics["peak_state_bytes_rows"] = len(history)
                mode_metrics["peak_state_bytes_full_pages"] = full_pages
            for key, count in (("peak_coverage_members", boundary["coverage_members"]),
                               ("peak_live_selected_members", boundary["live_selected_members"]),
                               ("peak_span_entries", boundary["span_entries"]),
                               ("peak_id_entries", boundary["id_entries"])):
                mode_metrics[key] = max(mode_metrics[key], count)
            mode_metrics["retired_entries"] += retired
            if retired:
                mode_metrics["retirement_pages"].append(requests)
                mode_metrics["retirements"].append({"page": requests, "entries": retired})
            detail[mode] = {"coverage_entries": boundary["coverage_entries"],
                            "coverage_bytes": boundary["coverage_bytes"],
                            "span_entries": boundary["span_entries"], "id_entries": boundary["id_entries"],
                            "coverage_members": boundary["coverage_members"],
                            "live_selected_members": boundary["live_selected_members"],
                            "pending_members": boundary["pending_members"],
                            "omitted_members": boundary["omitted_members"],
                            "state_bytes": state_bytes,
                            "state_overhead_bytes": state_bytes - boundary["coverage_bytes"],
                            "retired_entries": retired}
            states[mode] = json.loads(_json(page.state))
        for mode in results:
            detail[mode]["state_overhead_bytes_vs_ids"] = (
                detail[mode]["state_overhead_bytes"] - detail["ids"]["state_overhead_bytes"]
                if "ids" in results else None)
        page_metrics.append(detail)
        if left.state is None:
            break

    return {"requests": requests, "full_pages": full_pages, "empty_pages": empty_pages,
            "rows": len(history), "represented_members": sum(len(row[2]) for row in history),
            "consumed": consumed_total, "fetched": fetched_total, "reads": reads_total,
            "page_size": page_size, "batch_size": batch_size, "candidate_budget": candidate_budget,
            "oracle_match": True, "replay_checked": bool(replay),
            "boundaries_audited": requests * len(representations), "representations": list(representations),
            "retirement_measure": "descriptors removed between returned page boundaries",
            "modes": metrics, "pages": page_metrics}
