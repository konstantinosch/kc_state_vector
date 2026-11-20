"""Three-way coverage comparison with optional explicit preceding evidence."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .audit import run_comparison
from .generated import distribution_scenarios, generated_scenarios
from .hybrid_scenarios import hybrid_scenarios
from .run_checks import SCHEDULES
from .reporting import regenerate_findings, render_findings
from .scenarios import targeted_scenarios


MODES = ("ids", "spans", "hybrid")
ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT.parent / "results/grouped/hybrid"


def _key(result):
    return tuple(result[key] for key in ("name", "plan_fingerprint", "page_size", "batch_size", "candidate_budget"))


def _assert_prior(expected, actual, location="result"):
    """New metrics may be added; every old result field must remain unchanged."""
    if isinstance(expected, dict):
        for key, value in expected.items():
            if key == "boundaries_audited":
                continue  # Three independently audited modes replace two.
            if key == "representations" and tuple(value) == ("ids", "spans"):
                assert tuple(actual[key]) == MODES, location + "/representations"
                continue  # Keep both old modes while adding the third comparison.
            _assert_prior(value, actual[key], location + "/" + key)
    elif isinstance(expected, list):
        assert len(expected) == len(actual), location
        for index, (left, right) in enumerate(zip(expected, actual)):
            _assert_prior(left, right, location + f"/{index}")
    else:
        assert expected == actual, (location, expected, actual)


def run(generated_cases=600, groups=200, members=12, seed=20261119, *, previous_comparison=None):
    prior = ({"results": []} if previous_comparison is None else
             json.loads(Path(previous_comparison).read_text(encoding="utf-8")))
    previous = {_key(result): result for result in prior["results"]}
    results, matched = [], 0
    suites = (("targeted", targeted_scenarios()),
              ("hybrid", hybrid_scenarios()),
              ("generated", generated_scenarios(generated_cases, seed)),
              ("long", distribution_scenarios(groups, members, seed)))
    for kind, scenarios in suites:
        for number, case in enumerate(scenarios, 1):
            for size, batch, budget in (SCHEDULES if kind != "long" else (SCHEDULES[-1],)):
                try:
                    result = run_comparison(case.plan, size, batch, budget, representations=MODES)
                    result.update(kind=kind, name=case.name, description=case.description,
                                  scoped_records=len(case.plan.records), plan_fingerprint=case.plan.fingerprint)
                    old = previous.get(_key(result))
                    if old is not None:
                        _assert_prior(old, result)
                        matched += 1
                    counts = {mode: 0 for mode in ("ids", "spans")}
                    examples = []
                    for page in result["pages"]:
                        for other in counts:
                            if page["hybrid"]["coverage_bytes"] > page[other]["coverage_bytes"]:
                                counts[other] += 1
                                if len(examples) < 4:
                                    examples.append({"request": page["page"], "rows_so_far": page["rows_so_far"],
                                                     "other": other, "other_coverage_bytes": page[other]["coverage_bytes"],
                                                     "hybrid_coverage_bytes": page["hybrid"]["coverage_bytes"]})
                    result["hybrid_larger_coverage_boundaries"] = counts
                    result["hybrid_larger_examples"] = examples
                    if kind not in ("long", "hybrid"):
                        result.pop("pages")
                    results.append(result)
                except Exception as exc:
                    raise AssertionError(f"{kind}/{case.name}, schedule={(size, batch, budget)}") from exc
            if kind != "generated" or number % 100 == 0:
                print(f"PASS {kind}: {number} plans", flush=True)
    return results, matched


def write_results(results, destination, config, previous_matches, *, previous_comparison=None):
    if previous_comparison is not None and (destination / "comparison.json").resolve() == Path(previous_comparison).resolve():
        raise ValueError("Choose an output directory that preserves the preceding comparison")
    destination.mkdir(parents=True, exist_ok=True)
    sources = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted(ROOT.rglob("*.py"))}
    previous_files = ({} if previous_comparison is None else {
        Path(previous_comparison).name: hashlib.sha256(Path(previous_comparison).read_bytes()).hexdigest()})
    totals = {"three_way_comparisons": len(results), "individual_traversals": len(results) * len(MODES),
              "audited_boundaries": sum(r["boundaries_audited"] for r in results),
              "requests_per_mode": sum(r["requests"] for r in results),
              "replay_calls": sum(r["requests"] * len(MODES) for r in results),
              "all_oracles_match": all(r["oracle_match"] for r in results),
              "previous_two_mode_comparisons_unchanged": previous_matches,
              "hybrid_larger_coverage_boundaries": {
                  mode: sum(r["hybrid_larger_coverage_boundaries"][mode] for r in results)
                  for mode in ("ids", "spans")}}
    report = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "Fixed-data grouped coverage comparison; correctness and serialized size only.",
              "config": config, "source_sha256": sources, "previous_results_sha256": previous_files,
              "totals": totals, "results": results}
    (destination / "comparison.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    columns = ("scenario", "kind", "request", "rows", "rows_so_far", "full_pages_so_far",
               "represented_members", "consumed", "mode", "coverage_entries", "coverage_bytes",
               "span_entries", "id_entries", "coverage_members", "live_selected_members", "pending_members",
               "omitted_members", "state_bytes", "state_overhead_bytes_vs_ids", "retired_entries", "next_stage")
    with (destination / "pages.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            for page in result.get("pages", ()):
                for mode in MODES:
                    writer.writerow({"scenario": result["name"], "kind": result["kind"], "request": page["page"],
                                     **{key: page[key] for key in ("rows", "rows_so_far", "full_pages_so_far",
                                                                  "represented_members", "consumed", "next_stage")},
                                     "mode": mode, **page[mode]})

    (destination / "FINDINGS.md").write_text(render_findings(report), encoding="utf-8")
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generated-cases", type=int, default=600)
    parser.add_argument("--groups", type=int, default=200)
    parser.add_argument("--members", type=int, default=12)
    parser.add_argument("--seed", type=int, default=20261119)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--previous-comparison", type=Path,
                        help="Optional earlier two-mode comparison.json; never read local research implicitly")
    parser.add_argument("--render-only", action="store_true",
                        help="Regenerate only FINDINGS.md from saved JSON; preserve all measurements")
    args = parser.parse_args()
    if args.render_only:
        print(f"Regenerated prose only: {regenerate_findings(args.output)}", flush=True)
        return
    if args.generated_cases < 0:
        parser.error("--generated-cases must be nonnegative")
    if args.previous_comparison is not None and (args.output / "comparison.json").resolve() == args.previous_comparison.resolve():
        parser.error("--output must not overwrite --previous-comparison")
    results, matched = run(args.generated_cases, args.groups, args.members, args.seed,
                          previous_comparison=args.previous_comparison)
    totals = write_results(results, args.output, {
        "generated_cases": args.generated_cases, "groups": args.groups,
        "members": args.members, "seed": args.seed, "schedules": SCHEDULES, "representations": MODES,
    }, matched, previous_comparison=args.previous_comparison)
    print(json.dumps(totals, indent=2), flush=True)


if __name__ == "__main__":
    main()
