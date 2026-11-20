"""Independent traversal comparisons for the placement reference.

Exact paths retain the public model's oracle and boundary audits. Approximate
recovery is checked against an independently constructed raw keyset tail, then
compared with the original result to expose the guarantees it has abandoned.
"""

from collections import Counter
import json

from .adapters import parameters


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _bound(adapter):
    lengths = [len(stage.candidate_ids if adapter.grouped else stage.items)
               for stage in adapter.plan.stages]
    return sum(lengths) * 2 + len(adapter.records) + len(lengths) + 10


def reference_trace(adapter, params):
    """Run the unchanged selection model, independent of placement protocol."""
    params = parameters(params)
    state, history, pages = None, [], []
    expected = adapter.expected_rows()
    for _ in range(_bound(adapter)):
        before = canonical(state)
        page = adapter.select(state, params)
        _require(canonical(state) == before, "model changed the incoming state")
        history.extend(page["rows"])
        _require(history == expected[:len(history)], "model differs from oracle prefix")
        adapter.audit(page["state"], history)
        pages.append(page)
        state = page["state"]
        if state is None:
            break
    else:
        raise AssertionError("model traversal did not terminate")
    _require(history == expected, "model differs from complete oracle")
    return pages


def represented_differences(expected, actual):
    """Count concrete member losses/repeats independently of stage/row labels."""
    expected_members = Counter(member for row in expected for member in row[2])
    actual_members = Counter(member for row in actual for member in row[2])
    repeated = {identity: count - 1 for identity, count in actual_members.items() if count > 1}
    omitted = expected_members.keys() - actual_members.keys()
    unexpected = actual_members.keys() - expected_members.keys()
    return {
        "duplicate_member_occurrences": sum(repeated.values()),
        "duplicated_members": len(repeated),
        "omitted_members": len(omitted),
        "unexpected_members": len(unexpected),
        "duplicate_examples": sorted(repeated)[:8],
        "omitted_examples": sorted(omitted)[:8],
        "unexpected_examples": sorted(unexpected)[:8],
        "same_ordered_members": [member for row in expected for member in row[2]] ==
                                [member for row in actual for member in row[2]],
        "same_representative_order": [row[1] for row in expected] == [row[1] for row in actual],
    }


def _state(service, continuation):
    return None if continuation is None else service.logical_state(continuation)


def healthy_run(adapter, threshold, params, baseline):
    """Compare every returned logical page/state/work count with the public model."""
    from .protocol import Service

    service = Service(adapter, threshold=threshold, policy="exact")
    token = service.start(chain="matrix-chain")
    initial_bytes = len(token.encode("utf-8"))
    rows, responses, metrics, spilled_requests = [], [], [], []
    full_pages = 0
    first_spill = None
    ever_spilled = False
    for index, expected in enumerate(baseline, 1):
        incoming = service.inspect(token)
        if incoming["kind"] == "handle":
            spilled_requests.append(index)
        response = service.request(token, **params)
        _require(response["status"] == "exact", "healthy run was unexpectedly degraded")
        _require(response["client_action"] == "append", "healthy run asked to replace results")
        _require(response["rows"] == expected["rows"], "placement changed exact rows")
        _require(response["work"] == expected["work"], "placement changed selection work counters")
        continuation = response["continuation"]
        logical = _state(service, continuation)
        _require(logical == expected["state"], "placement changed the logical continuation")
        rows.extend(response["rows"])
        full_pages += len(response["rows"]) == params["page_size"]
        kind = "terminal" if continuation is None else service.inspect(continuation)["kind"]
        token_bytes = 0 if continuation is None else len(continuation.encode("utf-8"))
        _require(token_bytes <= threshold, "outgoing token exceeded its complete-wire threshold")
        if kind == "handle":
            if first_spill is None:
                first_spill = {"request": index, "rows_so_far": len(rows)}
            ever_spilled = True
        if ever_spilled:
            _require(kind != "inline", "ordinary continuation moved back from stored to inline")
        metrics.append({"request": index, "rows": len(response["rows"]),
                        "rows_so_far": len(rows), "full_pages_so_far": full_pages,
                        "kind": kind, "token_bytes": token_bytes,
                        "server_bytes": service.store.storage_bytes()})
        responses.append(response)
        token = continuation
        if token is None:
            _require(index == len(baseline), "placement terminated earlier than model")
            break
    _require(token is None, "placement failed to terminate with the model")
    _require(rows == adapter.expected_rows(), "healthy placement differs from complete oracle")
    counts = Counter(row["kind"] for row in metrics)
    nonterminal = counts["inline"] + counts["handle"]
    peak = max(metrics, key=lambda row: row["server_bytes"])
    return {
        "responses": responses, "pages": metrics, "handle_input_requests": spilled_requests,
        "summary": {"requests": len(metrics), "rows": len(rows),
                    "represented_members": sum(len(row[2]) for row in rows),
                    "inline_pages": counts["inline"], "spilled_pages": counts["handle"],
                    "terminal_pages": counts["terminal"],
                    "inline_fraction_nonterminal": counts["inline"] / nonterminal if nonterminal else None,
                    "spilled_fraction_nonterminal": counts["handle"] / nonterminal if nonterminal else None,
                    "first_spill": first_spill, "initial_token_bytes": initial_bytes,
                    "peak_token_bytes": max([initial_bytes] + [row["token_bytes"] for row in metrics]),
                    "peak_server_bytes": peak["server_bytes"], "peak_server_request": peak["request"],
                    "oracle_match": True, "logical_boundaries_audited": len(baseline)},
    }


def exact_eviction_run(adapter, threshold, params, healthy):
    """Evict volatile state at every handle request; require byte-identical responses."""
    from .protocol import Service

    service = Service(adapter, threshold=threshold, policy="exact")
    token = service.start(chain="matrix-chain")
    evictions = 0
    for expected in healthy["responses"]:
        incoming = service.inspect(token)
        if incoming["kind"] == "handle":
            service.store.drop_hot(incoming["chain"])
            evictions += 1
        response = service.request(token, **params)
        _require(canonical(response) == canonical(expected), "exact recovery changed the complete response")
        token = response["continuation"]
    _require(token is None, "exact recovery did not finish")
    _require(evictions == len(healthy["handle_input_requests"]), "requested eviction did not execute")
    return {"requests": len(healthy["responses"]), "hot_evictions": evictions,
            "byte_identical_responses": True}


def _raw_tail(adapter, cursor):
    """Do not call Adapter.best_effort: derive the fallback ordering directly."""
    records = sorted(adapter.records, key=lambda record: (record.sort_key, record.id), reverse=True)
    return [["best_effort", record.id, [record.id]] for record in records
            if cursor is None or (record.sort_key, record.id) < tuple(cursor)]


def fallback_run(adapter, threshold, params, healthy, request_number, policy):
    """Lose both vector copies once; check explicit fallback and its retry."""
    from .protocol import Service

    _require(policy in ("best_effort", "restart"), "unknown fallback policy")
    _require(request_number in healthy["handle_input_requests"], "loss point is not a stored checkpoint")
    service = Service(adapter, threshold=threshold, policy=policy)
    token = service.start(chain="matrix-chain")
    prefix = []
    for index in range(1, request_number):
        response = service.request(token, **params)
        _require(response["rows"] == healthy["responses"][index - 1]["rows"], "pre-loss rows changed")
        prefix.extend(response["rows"])
        token = response["continuation"]
    incoming = service.inspect(token)
    _require(incoming["kind"] == "handle", "failure injection did not reach a handle")
    service.store.lose_state(incoming["chain"])
    lost_input = token
    first = service.request(token, **params)
    _require(first["status"] == policy, "fallback did not declare changed semantics")
    _require(first["chain"] != incoming["chain"], "fallback did not open a distinct chain")
    _require(first["client_action"] == ("replace" if policy == "restart" else "append"),
             "fallback gave the wrong client list action")
    retry = service.request(lost_input, **params)
    _require(canonical(retry) == canonical(first), "fallback retry changed its accepted response")
    response, tail, response_count = first, [], 0
    expected_tail = adapter.expected_rows() if policy == "restart" else _raw_tail(adapter, incoming["anchor"])
    for index in range(_bound(adapter)):
        response_count += 1
        tail.extend(response["rows"])
        _require(tail == expected_tail[:len(tail)], "fallback differs from its independently defined result")
        if policy == "restart":
            adapter.audit(_state(service, response["continuation"]), tail)
            _require(index == 0 or response["status"] == "exact", "new exact chain kept claiming restart")
        else:
            _require(response["status"] == "best_effort", "approximate continuation lost its label")
        continuation = response["continuation"]
        if continuation is None:
            break
        _require(len(continuation.encode("utf-8")) <= threshold, "fallback token exceeded threshold")
        response = service.request(continuation, **params)
        _require(response["client_action"] == "append", "follow-up recovery page unexpectedly replaced list")
    else:
        raise AssertionError("fallback traversal did not terminate")
    _require(tail == expected_tail, "fallback missed part of its declared result")
    visible = tail if policy == "restart" else prefix + tail
    original = adapter.expected_rows()
    return {"policy": policy, "loss_before_request": request_number,
            "rows_before_loss": len(prefix), "anchor": incoming["anchor"],
            "post_loss_requests": response_count, "post_loss_rows": len(tail),
            "client_action": first["client_action"], "new_chain": True,
            "retry_identical": True, "declared_result_match": True,
            "visible_rows": len(visible), **represented_differences(original, visible)}


def run_case(adapter, threshold=2048, params=None, *, loss_points=3, baseline=None):
    """One plan/encoding/threshold, with every hot miss and bounded total-loss sites."""
    params = parameters({} if params is None else params)
    if type(loss_points) is not int or loss_points < 0:
        raise ValueError("loss_points must be a nonnegative integer")
    baseline = reference_trace(adapter, params) if baseline is None else baseline
    healthy = healthy_run(adapter, threshold, params, baseline)
    exact = exact_eviction_run(adapter, threshold, params, healthy)
    candidates = healthy["handle_input_requests"]
    if not loss_points or not candidates:
        chosen = []
    elif loss_points == 1:
        chosen = [candidates[0]]
    else:
        chosen = sorted({candidates[index * (len(candidates) - 1) // (loss_points - 1)]
                         for index in range(loss_points)})
    fallbacks = [fallback_run(adapter, threshold, params, healthy, point, policy)
                 for point in chosen for policy in ("best_effort", "restart")]
    return {"threshold_bytes": threshold, "params": params,
            "healthy": healthy["summary"], "pages": healthy["pages"],
            "exact_recovery": exact, "fallbacks": fallbacks,
            "individual_traversals": 2 + len(fallbacks),
            "all_checks_passed": True}
