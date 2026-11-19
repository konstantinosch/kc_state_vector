"""Execute finite tiny-plan domains; keep summaries and replayable failures local.

Run all domains with `python -m reference.exhaustive`, or name one with
--family. --limit is a smoke run and is never reported as exhaustive. Three
representative execution profiles are used, not every possible request history.
"""

import argparse
import hashlib
from itertools import islice
import json
from pathlib import Path

from .tiny_checker import EXECUTION_PROFILES, validate_case
from .tiny_plans import BOUNDS, EXPECTED_COUNTS, FAMILIES, TinyCase, iter_cases


DEFAULT_OUTPUT = Path(__file__).parent / "results" / "exhaustive"


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _source_hashes():
    root = Path(__file__).parent
    names = ("model.py", "oracle.py", "retirement.py", "tiny_plans.py", "tiny_checker.py", "exhaustive.py")
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}


def run_family(family, output=DEFAULT_OUTPUT, *, limit=None, progress_every=2000):
    expected = EXPECTED_COUNTS[family]
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("A smoke limit must be a positive integer")
    totals = {profile.name: {"traversals": 0, "requests": 0, "audited_boundaries": 0,
                             "empty_pages": 0, "selected_rows": 0} for profile in EXECUTION_PROFILES}
    cases = iter_cases(family)
    if limit is not None:
        cases = islice(cases, limit)
    completed = 0
    hashes = _source_hashes()
    _write_json(output / f"{family}.json",
                {"schema_version": 1, "family": family, "exhaustive": False,
                 "status": "running", "source_sha256": hashes})
    for index, case in enumerate(cases, 1):
        plan = case.to_plan()
        for profile in EXECUTION_PROFILES:
            try:
                metrics = validate_case(plan, profile)
            except Exception as exc:
                artifact = {"schema_version": 1, "family": family, "case_index": index,
                            "profile": profile.name, "case": case.to_dict(),
                            "failure": f"{type(exc).__name__}: {exc}", "source_sha256": hashes}
                path = output / f"failure-{family}.json"
                _write_json(path, artifact)
                _write_json(output / f"{family}.json",
                            {"schema_version": 1, "family": family, "exhaustive": False,
                             "status": "failed", "completed_configurations": completed,
                             "failure_fixture": path.name, "source_sha256": hashes})
                raise AssertionError(f"{family} case {index}, {profile.name}; replay fixture: {path}") from exc
            values = totals[profile.name]
            values["traversals"] += 1
            for key in ("requests", "audited_boundaries", "empty_pages", "selected_rows"):
                values[key] += metrics[key]
        completed += 1
        if progress_every and completed % progress_every == 0:
            print(f"PASS {family}: {completed:,}/{expected:,} configurations, "
                  f"{completed * len(EXECUTION_PROFILES):,} traversals", flush=True)
    if limit is None and completed != expected:
        raise AssertionError(f"{family}: enumeration count {completed} != {expected}")
    result = {"schema_version": 1, "family": family, "bounds": BOUNDS[family], "status": "passed",
              "exhaustive": limit is None and completed == expected, "requested_limit": limit,
              "expected_configurations": expected, "configurations": completed,
              "total_traversals": sum(value["traversals"] for value in totals.values()),
              "total_audited_boundaries": sum(value["audited_boundaries"] for value in totals.values()),
              "profiles": totals, "source_sha256": hashes}
    _write_json(output / f"{family}.json", result)
    print(f"{'EXHAUSTIVE PASS' if result['exhaustive'] else 'PARTIAL PASS'} {family}: "
          f"{completed:,} configurations; {result['total_traversals']:,} traversals; "
          f"{result['total_audited_boundaries']:,} audited boundaries", flush=True)
    return result


def summarize(output):
    results = [json.loads((output / f"{family}.json").read_text(encoding="utf-8")) for family in FAMILIES]
    for family, result in zip(FAMILIES, results):
        if result["family"] != family or not result["exhaustive"] or result["configurations"] != EXPECTED_COUNTS[family]:
            raise ValueError(f"Incomplete or mismatched result for {family}")
        if result["source_sha256"] != _source_hashes():
            raise ValueError(f"Source files changed since {family} ran; its result is historical")
    configurations = sum(value["configurations"] for value in results)
    traversals = sum(value["total_traversals"] for value in results)
    boundaries = sum(value["total_audited_boundaries"] for value in results)
    combined = {"schema_version": 1, "configurations": configurations, "traversals": traversals,
                "audited_boundaries": boundaries, "all_domains_exhaustive": True, "families": results}
    _write_json(output / "summary.json", combined)
    lines = ["# Local exhaustive tiny-plan results", "",
             f"**{configurations:,} declaration configurations; {traversals:,} complete traversals; {boundaries:,} audited boundaries.**", "",
             "Every declaration combination inside the following bounds passed. Configurations need not be behaviorally distinct: some quotas and losing claims produce the same result.", "",
             "| Domain | Configurations | Traversals | Audited boundaries |",
             "| --- | ---: | ---: | ---: |"]
    for result in results:
        lines.append(f"| {result['family']} | {result['configurations']:,} | {result['total_traversals']:,} | {result['total_audited_boundaries']:,} |")
    lines.extend(["", "## Exact enumeration bounds", ""])
    for family in FAMILIES:
        lines.append(f"- **{family}:** {BOUNDS[family]}")
    lines.extend(["", "All stages share the canonical total order C > B > A, where present, with tied sort keys. Owners may be at any stage. Reservation sets are nonempty, and yielding sets are explicit; `None` is an equivalent spelling tested separately.",
                  "", "## Execution profiles", "",
                  "| Profile | Prepared buffer sizes by stage | Page sizes | Batch sizes | Candidate budgets |",
                  "| --- | --- | --- | --- | --- |"])
    for profile in EXECUTION_PROFILES:
        lines.append(f"| {profile.name} | {profile.buffer_sizes} | {profile.page_sizes} | {profile.batch_sizes} | {profile.candidate_budgets} |")
    lines.extend(["", "Tuples repeat across requests. Every traversal compares ordered (stage, ID) prefixes with the independent unpaged oracle, audits retained state after every request, restarts the pager with JSON-round-tripped state, checks input immutability and cursor progress, and must terminate within a finite candidate bound.",
                  "", "## Limits", "",
                  "This is exhaustive over the declared plan domains, not arbitrary data sizes, numeric keys, request histories or malformed inputs. Multi-owner conflicts are exhaustive only for one ID. Larger multi-item conflicts are covered by the separate targeted, seeded and long-run tests. These runs assume fixed data and sufficient capacity; failures, transport security, live writes, grouped coverage and engine performance are separate checks.",
                  "", "No model, oracle or public repository changes are required by a passing run. Source SHA-256 values and aggregate counters are recorded in `summary.json`; a failing run saves its concrete configuration and execution profile for replay.", ""])
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Complete summary: {output / 'REPORT.md'}")
    return combined


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", choices=FAMILIES)
    parser.add_argument("--limit", type=int, help="partial smoke run; never labelled exhaustive")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summarize", action="store_true", help="combine existing complete family reports")
    parser.add_argument("--replay", type=Path, help="replay a saved failure fixture")
    args = parser.parse_args()
    if args.replay:
        artifact = json.loads(args.replay.read_text(encoding="utf-8"))
        profile = next(value for value in EXECUTION_PROFILES if value.name == artifact["profile"])
        print(validate_case(TinyCase.from_dict(artifact["case"]).to_plan(), profile))
    elif args.summarize:
        summarize(args.output)
    else:
        for family in (args.family,) if args.family else FAMILIES:
            run_family(family, args.output, limit=args.limit)
        if args.family is None and args.limit is None:
            summarize(args.output)


if __name__ == "__main__":
    main()
