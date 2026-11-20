"""Check a local pager against fixed staged fixtures; no network or third-party packages.

Adapters are trusted Python code. Worker isolation bounds hangs and diagnostics, not privileges.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import importlib.util
import itertools
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

if not __debug__:
    raise SystemExit("self-check requires assertions enabled; remove -O/-OO and PYTHONOPTIMIZE")

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("_kc_self_check_oracle", HERE / "oracle.py")
_oracle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_oracle)
validate_fixture = _oracle.validate_fixture
load_fixture = _oracle.load_fixture
unpaged_oracle = _oracle.unpaged_oracle

CAPABILITIES = frozenset({"batch_size", "candidate_budget", "stage_labels"})
FIXTURES = ("priority_51_49_48", "full_tuple_ties", "decimal_id_ties",
            "empty_duplicate_page", "three_overlapping_stages", "unread_batch_tail",
            "empty_plan", "empty_sources", "zero_quotas", "all_zero_quotas")
PROFILE = {"data": "fixed finite staged sources; no reservations or grouping",
           "order": "descending (integer sort_key, ordinal printable ASCII id)",
           "mapping": "other orders/key types require an order-preserving adapter mapping",
           "work": "optional raw-candidate counts are adapter-reported, not instrumented",
           "limits": "no proof of query cost, minimal state, live consistency or production suitability"}
MAX_STATE = 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
MAX_DIAGNOSTICS = 64 * 1024
MAX_CONTROL = 8 * 1024 * 1024


class CheckFailure(Exception):
    def __init__(self, kind, message, **details):
        super().__init__(message)
        self.details = {"kind": kind, "message": message, **details}


class CheckError(CheckFailure):
    pass


def canonical(value, *, max_depth=64):
    def inspect(item, depth=0):
        if type(item) in (dict, list) and depth >= max_depth:
            raise CheckError("size_limit", f"JSON nesting exceeds {max_depth} containers")
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError("JSON object keys must be strings")
            for child in item.values():
                inspect(child, depth + 1)
        elif type(item) is list:
            for child in item:
                inspect(child, depth + 1)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("Only strict JSON values are supported")
    inspect(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def capabilities(adapter):
    values = getattr(adapter, "CAPABILITIES", set())
    if type(values) not in (set, frozenset, tuple, list) or any(type(v) is not str for v in values):
        raise CheckError("configuration", "CAPABILITIES must contain supported strings")
    if len(set(values)) != len(values) or not set(values) <= CAPABILITIES:
        raise CheckError("configuration", "Unknown or repeated capability")
    if not callable(getattr(adapter, "make_pager", None)):
        raise CheckError("configuration", "Adapter must export make_pager(fixture)")
    return frozenset(values)


def checks(caps):
    run = ["id_sequence", "termination", "retry", "input_immutability", "progress", "continuation_roundtrip"]
    skipped = {}
    for cap, check in (("batch_size", "batch_variation"), ("candidate_budget", "candidate_budget"),
                       ("stage_labels", "stage_attribution")):
        if cap in caps:
            run.append(check)
        else:
            skipped[check] = f"Adapter does not declare {cap}"
    return run, skipped


def _positive(value, label, maximum=None):
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        raise CheckError("configuration", f"{label} must be a positive integer" +
                         (f" <= {maximum}" if maximum else ""))
    return value


def _schedule(value):
    if type(value) is not dict or type(value.get("name")) is not str:
        raise CheckError("configuration", "Schedule needs a name and parameter lists")
    for name in ("page_sizes", "batch_sizes", "candidate_budgets"):
        values = value.get(name)
        if type(values) is not list or not values:
            raise CheckError("configuration", f"{name} must be a nonempty list")
        for v in values:
            _positive(v, name, 1000)
    return value


def _params(schedule, step, caps):
    result = {"page_size": schedule["page_sizes"][step % len(schedule["page_sizes"]) ]}
    for cap, key in (("batch_size", "batch_sizes"), ("candidate_budget", "candidate_budgets")):
        if cap in caps:
            result[cap] = schedule[key][step % len(schedule[key])]
    return result


def _unchanged(value, encoded):
    try:
        return canonical(value) == encoded
    except (TypeError, ValueError, RecursionError, CheckError):
        return False


def _call(adapter, fixture, state, params):
    source, incoming = deepcopy(fixture), deepcopy(state)
    source_before, state_before = canonical(source), canonical(incoming)
    error = None
    try:
        page = adapter.make_pager(source)
        if not _unchanged(source, source_before):
            raise CheckFailure("input_mutation", "Factory changed its input fixture")
        if not callable(page):
            raise CheckError("configuration", "make_pager must return a callable")
        response = page(incoming, **params)
    except Exception as exc:
        error = exc
    if not _unchanged(source, source_before) or not _unchanged(incoming, state_before):
        raise CheckFailure("input_mutation", "Adapter changed its fixture or input continuation")
    if isinstance(error, CheckFailure):
        raise error
    if error is not None:
        raise CheckError("adapter_exception", f"{type(error).__name__}: {error}")
    return response


def _response(response, params, caps):
    if type(response) is not dict or not {"rows", "continuation"} <= response.keys() or \
            not set(response) <= {"rows", "continuation", "consumed"}:
        raise CheckFailure("invalid_response", "Response requires rows and continuation, with optional consumed")
    try:
        encoded = canonical(response, max_depth=65)
    except CheckError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise CheckFailure("invalid_response", f"Response is not strict JSON: {exc}")
    if len(encoded.encode("ascii")) > MAX_RESPONSE:
        raise CheckError("size_limit", "Response exceeds 2 MiB")
    rows, state = response["rows"], response["continuation"]
    if type(rows) is not list:
        raise CheckFailure("invalid_response", "rows must be a list")
    if len(rows) > params["page_size"]:
        raise CheckFailure("bounds", "Page exceeds page_size")
    if state is not None and not (type(state) is dict or (type(state) is str and state)):
        raise CheckFailure("invalid_response", "Continuation must be null, an object or a nonempty string")
    if len(canonical(state).encode("ascii")) > MAX_STATE:
        raise CheckError("size_limit", "Continuation exceeds 1 MiB")
    for row in rows:
        valid = type(row) is str and bool(row)
        labeled = type(row) is dict and set(row) == {"stage", "id"} and \
            all(type(row[k]) is str and row[k] for k in ("stage", "id"))
        if not (labeled if "stage_labels" in caps else valid or labeled):
            raise CheckFailure("invalid_response", "Rows must be IDs or {stage,id}; stage_labels requires objects")
    if "candidate_budget" in caps:
        count = response.get("consumed")
        if type(count) is not int:
            raise CheckFailure("invalid_response", "candidate_budget requires integer consumed")
        if not len(rows) <= count <= params["candidate_budget"]:
            raise CheckFailure("bounds", "consumed must lie between selected rows and candidate_budget")
        if state is not None and len(rows) < params["page_size"] and count != params["candidate_budget"]:
            raise CheckFailure("bounds", "A short raw-candidate page must spend its candidate budget")
    return json.loads(encoded)


def _project(row, caps):
    return row if "stage_labels" in caps else row["id"] if type(row) is dict else row


def evaluate(adapter, fixture, schedule, *, max_requests=None):
    """In-process behavioral checker for tests. CLI uses killable workers around this function."""
    result = {"fixture": fixture.get("name") if type(fixture) is dict else None,
              "schedule": schedule, "profile": PROFILE, "status": "error", "exit_code": 2,
              "requests": 0, "rows": 0}
    try:
        validate_fixture(fixture)
        _schedule(schedule)
        caps = capabilities(adapter)
        result["capabilities"] = sorted(caps)
        result["checks_run"], result["checks_skipped"] = checks(caps)
        limit = max_requests if max_requests is not None else sum(len(s["ids"]) for s in fixture["stages"]) + len(fixture["stages"]) + 1
        _positive(limit, "max_requests")
        result["max_requests"] = limit
        expected = unpaged_oracle(fixture)
        state, offset, visited = None, 0, set()
        result["empty_nonterminal_pages"] = 0
        for step in range(limit):
            result["requests"] = step + 1
            params = _params(schedule, step, caps)
            response = _response(_call(adapter, fixture, state, params), params, caps)
            rows = response["rows"]
            for position, actual in enumerate(rows):
                context = {"request": step + 1, "page_position": position + 1,
                           "global_position": offset + position + 1, "parameters": params}
                if offset + position >= len(expected):
                    raise CheckFailure("extra_row", "Result contains an extra row", expected="END", actual=actual, **context)
                wanted = expected[offset + position]
                if _project(actual, caps) != _project(wanted, caps):
                    raise CheckFailure("row_mismatch", "First selection mismatch", expected=_project(wanted, caps),
                                       actual=_project(actual, caps), **context)
            replay = _response(_call(adapter, fixture, state, params), params, caps)
            if canonical(replay, max_depth=65) != canonical(response, max_depth=65):
                raise CheckFailure("retry_mismatch", "Same input and parameters produced a different response")
            if step == 1:
                result["resume_witness"] = {"input": deepcopy(state), "parameters": params, "response": response}
            offset += len(rows)
            result["rows"] = offset
            following = response["continuation"]
            if following is None:
                if offset != len(expected):
                    raise CheckFailure("early_exhaustion", "Terminal response omitted an expected row",
                                       expected=_project(expected[offset], caps), actual="END", request=step + 1,
                                       page_position=len(rows) + 1, global_position=offset + 1, parameters=params)
                result.update(status="passed", exit_code=0)
                return result
            encoded = canonical(following)
            if encoded in visited or encoded == canonical(state):
                raise CheckFailure("nonprogress", "Nonterminal continuation repeated")
            visited.add(encoded)
            if not rows:
                result["empty_nonterminal_pages"] += 1
            state = json.loads(encoded)
        raise CheckError("request_limit", "Traversal did not complete within its request guard")
    except CheckFailure as exc:
        result.update(status="error" if isinstance(exc, CheckError) else "failed",
                      exit_code=2 if isinstance(exc, CheckError) else 1, failure=exc.details)
        result["failure"].setdefault("request", result["requests"])
    except Exception as exc:
        result.update(status="error", exit_code=2,
                      failure={"kind": "configuration", "message": f"{type(exc).__name__}: {exc}"})
    return result


def _load_adapter(path):
    path = Path(path).resolve()
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("_kc_candidate_adapter", path)
    if spec is None or spec.loader is None:
        raise CheckError("configuration", "Adapter must name a Python file")
    adapter = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = adapter
    spec.loader.exec_module(adapter)
    capabilities(adapter)
    return adapter


def _worker(request_path, output_path):
    try:
        request = json.loads(Path(request_path).read_text(encoding="utf-8"))
        adapter = _load_adapter(request["adapter"])
        caps = capabilities(adapter)
        if request["operation"] == "metadata":
            result = {"status": "passed", "exit_code": 0, "capabilities": sorted(caps)}
        elif request["operation"] == "resume":
            validate_fixture(request["fixture"])
            response = _response(_call(adapter, request["fixture"], request["input"], request["parameters"]),
                                 request["parameters"], caps)
            result = {"status": "passed", "exit_code": 0, "response": response}
        else:
            result = evaluate(adapter, request["fixture"], request["schedule"], max_requests=request.get("max_requests"))
    except Exception as exc:
        result = {"status": "error", "exit_code": 2, "failure":
                  exc.details if isinstance(exc, CheckFailure) else
                  {"kind": "adapter_exception", "message": f"{type(exc).__name__}: {exc}"}}
    # Worker envelopes add nesting around an independently bounded continuation.
    Path(output_path).write_text(canonical(result, max_depth=72), encoding="utf-8")


def _error(kind, message):
    return {"status": "error", "exit_code": 2, "failure": {"kind": kind, "message": message}}


def _launch(request, timeout, deadline=None):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        return _error("configuration", "timeout must be positive and finite")
    remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
    if remaining <= 0:
        return _error("timeout", "Whole invocation deadline reached")
    with tempfile.TemporaryDirectory(prefix="kc-pager-check-") as folder:
        root = Path(folder)
        incoming, outgoing = root / "request.json", root / "response.json"
        incoming.write_text(canonical(request, max_depth=72), encoding="utf-8")
        environment = dict(os.environ)
        for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE"):
            environment.pop(key, None)
        command = [sys.executable, "-S", "-B", "-X", "utf8", str(Path(__file__).resolve()),
                   "--worker", str(incoming), str(outgoing)]
        process = subprocess.Popen(command, cwd=root, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        diagnostic, overflow = bytearray(), threading.Event()
        def drain():
            while block := process.stdout.read(4096):
                available = MAX_DIAGNOSTICS - len(diagnostic)
                diagnostic.extend(block[:max(available, 0)])
                if len(block) > available:
                    overflow.set()
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        until = time.monotonic() + remaining
        problem = None
        while process.poll() is None:
            if overflow.is_set():
                problem = _error("output_limit", "Adapter diagnostics exceed 64 KiB")
            elif time.monotonic() >= until:
                problem = _error("timeout", "Worker deadline reached; validation incomplete")
            elif outgoing.exists() and outgoing.stat().st_size > MAX_CONTROL:
                problem = _error("size_limit", "Worker control response exceeds its limit")
            if problem:
                process.kill()
                break
            overflow.wait(0.01)
        process.wait()
        reader.join(timeout=1)
        if not reader.is_alive():
            process.stdout.close()
        if problem:
            return problem
        if reader.is_alive():
            return _error("worker_error", "Worker diagnostics did not finish; validation incomplete")
        if overflow.is_set():
            return _error("output_limit", "Adapter diagnostics exceed 64 KiB")
        if process.returncode or not outgoing.is_file():
            return _error("worker_error", f"Worker exited {process.returncode}: " + diagnostic.decode("utf-8", "replace")[-2000:])
        if outgoing.stat().st_size > MAX_CONTROL:
            return _error("size_limit", "Worker control response exceeds its limit")
        try:
            return json.loads(outgoing.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return _error("worker_error", f"Invalid worker response: {exc}")


def run_case(adapter_path, fixture, schedule, *, max_requests=None, timeout=300, deadline=None):
    request = {"operation": "evaluate", "adapter": str(Path(adapter_path).resolve()),
               "fixture": fixture, "schedule": schedule, "max_requests": max_requests}
    try:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            return _error("configuration", "timeout must be positive and finite")
        case_deadline = time.monotonic() + timeout
        if deadline is not None:
            case_deadline = min(case_deadline, deadline)
        result = _launch(request, timeout, case_deadline)
        witness = result.pop("resume_witness", None)
        if result["status"] == "passed" and witness is not None:
            replay = _launch({"operation": "resume", "adapter": request["adapter"], "fixture": fixture,
                              "input": witness["input"], "parameters": witness["parameters"]}, timeout, case_deadline)
            if replay["status"] != "passed":
                return {**result, "status": "error", "exit_code": 2, "failure": replay["failure"]}
            if canonical(replay["response"], max_depth=65) != canonical(witness["response"], max_depth=65):
                return {**result, "status": "failed", "exit_code": 1,
                        "failure": {"kind": "retry_mismatch", "message": "Fresh worker could not resume the checkpoint"}}
            result["checks_run"].append("fresh_process_resume")
            result["fresh_worker_resume"] = "checked"
        elif result["status"] == "passed":
            result["checks_skipped"]["fresh_process_resume"] = "Single-response traversal has no resume checkpoint"
            result["fresh_worker_resume"] = "not_applicable"
        return result
    except (OSError, ValueError, TypeError, CheckFailure) as exc:
        return _error("configuration", str(exc))


def schedules(caps, maximum=8):
    _positive(maximum, "max_page_size", 1000)
    pages = sorted(set(range(1, maximum + 1)) | {50})
    batches = (1, 3, 50) if "batch_size" in caps else (50,)
    budgets = (1, 2, 50) if "candidate_budget" in caps else (50,)
    result = [{"name": f"p{p}-b{b}-c{c}", "page_sizes": [p], "batch_sizes": [b], "candidate_budgets": [c]}
              for p, b, c in itertools.product(pages, batches, budgets)]
    for name, p, b, c in (("changing", [1,3,7,50], [3,1,50], [1,2,50]),
                           ("changing-reverse", [50,1,7,3], [50,3,1], [1,50,2]),
                           ("priority-witness", [3], [3], [50]),
                           ("duplicate-witness", [2], [3], [2,1,1,1]),
                           ("unread-tail-witness", [1], [3], [50])):
        result.append({"name": name, "page_sizes": p, "batch_sizes": b, "candidate_budgets": c})
    return result


def run(adapter_path, *, extra_fixtures=(), max_page_size=8, max_requests=None, timeout=300, run_timeout=600):
    report = {"status": "error", "exit_code": 2, "profile": PROFILE, "results": [],
              "guards": {"fixture_file_bytes": 4 * 1024 * 1024, "records": 10000, "stages": 32,
                         "fixture_files": 64, "request_parameter_maximum": 1000,
                         "continuation_bytes": MAX_STATE, "response_bytes": MAX_RESPONSE,
                         "json_container_depth": 64, "diagnostic_bytes": MAX_DIAGNOSTICS},
              "configuration": {"max_page_size": max_page_size, "max_requests": max_requests,
                                "timeout": timeout, "run_timeout": run_timeout}}
    try:
        _positive(max_page_size, "max_page_size", 1000)
        if max_requests is not None:
            _positive(max_requests, "max_requests")
        for name, value in (("timeout", timeout), ("run_timeout", run_timeout)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise CheckError("configuration", f"{name} must be positive and finite")
        adapter_path = Path(adapter_path).resolve()
        source_hash = hashlib.sha256(adapter_path.read_bytes()).hexdigest()
        report["adapter_sha256"] = source_hash
        paths = [HERE / "fixtures" / f"{name}.json" for name in FIXTURES] + [Path(p).resolve() for p in extra_fixtures]
        if len(paths) > 64:
            raise CheckError("configuration", "At most 64 fixture files, including the required suite")
        fixtures = [load_fixture(p) for p in paths]
        if len({f["name"] for f in fixtures}) != len(fixtures):
            raise CheckError("configuration", "Fixture names must be unique across the suite")
        report["fixtures"] = [{"name": f["name"], "sha256": hashlib.sha256(canonical(f).encode()).hexdigest(),
                                "bundled": i < len(FIXTURES)} for i, f in enumerate(fixtures)]
        deadline = time.monotonic() + run_timeout
        metadata = _launch({"operation": "metadata", "adapter": str(adapter_path)}, timeout, deadline)
        if metadata["status"] != "passed":
            report.update(failure=metadata["failure"])
            return report
        caps = frozenset(metadata["capabilities"])
        report["capabilities"] = sorted(caps)
        report["checks_run"], report["checks_skipped"] = checks(caps)
        selected = schedules(caps, max_page_size)
        report["scheduled_traversals"] = len(selected) * len(fixtures)
        for fixture in fixtures:
            for schedule in selected:
                result = run_case(adapter_path, fixture, schedule, max_requests=max_requests, timeout=timeout, deadline=deadline)
                result.setdefault("fixture", fixture["name"])
                result.setdefault("schedule", schedule)
                report["results"].append(result)
                if result.get("capabilities", sorted(caps)) != sorted(caps):
                    result.update(status="error", exit_code=2, failure={"kind":"configuration", "message":"Capabilities changed during run"})
                if result["status"] != "passed":
                    report.update(status=result["status"], exit_code=result["exit_code"], failure=result["failure"])
                    return report
        if hashlib.sha256(adapter_path.read_bytes()).hexdigest() != source_hash:
            raise CheckError("configuration", "Adapter source changed during the check")
        report.update(status="passed", exit_code=0)
    except CheckFailure as exc:
        report["failure"] = exc.details
    except Exception as exc:
        report["failure"] = {"kind": "configuration", "message": f"{type(exc).__name__}: {exc}"}
    return report


def _print_report(report):
    results = report["results"]
    total = report.get("scheduled_traversals", 0)
    status = report["status"].upper()
    active = results[-1] if results and results[-1]["status"] != "passed" else None
    if report["status"] == "passed":
        print(f"PASSED (scoped): all {len(results)} scheduled traversals completed")
    elif active is not None:
        reason = "stopped at first failure" if report["status"] == "failed" else "validation incomplete"
        print(f"{status} at traversal {len(results)} of {total} ({reason})")
    elif results:
        noun = "traversal" if len(results) == 1 else "traversals"
        print(f"{status} after {len(results)} completed {noun} (validation incomplete)")
    else:
        print(f"{status} before traversal execution (validation incomplete)")

    if "failure" in report:
        failure = report["failure"]
        context = []
        if active is not None:
            context.extend((active.get("fixture", "unknown fixture"),
                            "schedule " + active.get("schedule", {}).get("name", "unknown")))
        for key, label in (("request", "request"), ("page_position", "page position"),
                           ("global_position", "global position")):
            if key in failure:
                context.append(f"{label} {failure[key]}")
        detail = f"{failure.get('kind', 'failure')}: {failure.get('message', '')}"
        if "expected" in failure and "actual" in failure:
            detail += (f"; expected {json.dumps(failure['expected'], ensure_ascii=True)}, "
                       f"got {json.dumps(failure['actual'], ensure_ascii=True)}")
        print((", ".join(context) + ": " if context else "") + detail)

    print("Capabilities:", (", ".join(report["capabilities"]) or "base IDs only")
          if "capabilities" in report else "not established")
    print("Checks enabled:", ", ".join(report.get("checks_run", [])) or "not established")
    for name, reason in report.get("checks_skipped", {}).items():
        print(f"Skipped {name}: {reason}")
    print("Profile:")
    for name, value in report.get("profile", PROFILE).items():
        print(f"  {name.capitalize()}: {value}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, action="append", default=[])
    parser.add_argument("--max-page-size", type=int, default=8)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--run-timeout", type=float, default=600)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    if args.json and (args.json.exists() or args.json.resolve().is_relative_to(HERE.parent)):
        parser.error("Report must be a new file outside the skill package")
    report = run(args.adapter, extra_fixtures=args.fixture, max_page_size=args.max_page_size,
                 max_requests=args.max_requests, timeout=args.timeout, run_timeout=args.run_timeout)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        with args.json.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=True, allow_nan=False)
            stream.write("\n")
    _print_report(report)
    return report["exit_code"]


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        _worker(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(main())
