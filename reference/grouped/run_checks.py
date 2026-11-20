"""Repeatable ID/span comparison, with independent boundary audits."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .audit import run_comparison
from .generated import distribution_scenarios, generated_scenarios
from .scenarios import targeted_scenarios


SCHEDULES = ((1, 1, 1), (3, 7, 4), (50, 50, 1000))
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "results/grouped/ids-spans"


def run(generated_cases=600, groups=200, members=12, seed=20261119):
    results = []
    for kind, scenarios in (("targeted", targeted_scenarios()),
                            ("generated", generated_scenarios(generated_cases, seed)),
                            ("long", distribution_scenarios(groups, members, seed))):
        for number, case in enumerate(scenarios, 1):
            for page_size, batch_size, budget in (SCHEDULES if kind != "long" else (SCHEDULES[-1],)):
                try:
                    result = run_comparison(case.plan, page_size, batch_size, budget)
                except Exception as exc:
                    raise AssertionError(f"{kind}/{case.name}, schedule={(page_size, batch_size, budget)}") from exc
                result.update(kind=kind, name=case.name, description=case.description,
                              scoped_records=len(case.plan.records), plan_fingerprint=case.plan.fingerprint)
                # Every boundary is audited; retain detailed long traces without
                # making a giant JSON artifact for tiny generated plans.
                if kind != "long":
                    result.pop("pages")
                results.append(result)
            if kind != "generated" or number % 100 == 0:
                print(f"PASS {kind}: {number} plans", flush=True)
    return results


def write_results(results, destination, config):
    destination.mkdir(parents=True, exist_ok=True)
    source_dir = Path(__file__).resolve().parent
    sources = {path.relative_to(source_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted(source_dir.rglob("*.py"))}
    totals = {
        "paired_traversals": len(results), "individual_traversals": len(results) * 2,
        "audited_boundaries": sum(result["boundaries_audited"] for result in results),
        "paired_requests": sum(result["requests"] for result in results),
        "full_pages_per_mode": sum(result["full_pages"] for result in results),
        "replay_calls": sum(result["requests"] * 2 for result in results),
        "all_oracles_match": all(result["oracle_match"] for result in results),
    }
    report = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "Fixed-data grouped coverage and reservations; correctness and serialized size only.",
              "config": config, "source_sha256": sources, "totals": totals, "results": results}
    (destination / "comparison.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    columns = ("scenario", "request", "rows", "represented_members", "consumed", "mode",
               "coverage_entries", "coverage_members", "live_selected_members", "pending_members",
               "omitted_members", "state_bytes", "retired_entries", "next_stage")
    with (destination / "long_pages.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            for page in result.get("pages", ()):
                for mode in ("ids", "spans"):
                    writer.writerow({"scenario": result["name"], "request": page["page"],
                                     "rows": page["rows"], "represented_members": page["represented_members"],
                                     "consumed": page["consumed"], "mode": mode,
                                     "next_stage": page["next_stage"], **page[mode]})
    lines = ["# Grouped coverage and reservations: comparison results", "",
             "Finite synthetic reference comparison; no database performance or universal compression claim.", "",
             f"- {totals['paired_traversals']:,} paired comparisons / {totals['individual_traversals']:,} ID and span traversals.",
             f"- {totals['audited_boundaries']:,} independently audited response boundaries, including terminal responses.",
             f"- {totals['replay_calls']:,} fresh-pager retry calls returned identical rows, work and continuation.",
             "- Every complete sequence and every output prefix matched the independent unpaged oracle.",
             "- All selected member sets stayed disjoint; stage quotas and page sizes count rows, not members.", "",
             "## Page-50 distributions", "",
             "Each row below is one ID/span comparison with page size 50. Request counts include empty work-limited responses.",
             "Peak entries count selected coverage only. Pending/omitted reservation IDs remain explicit in both modes.",
             "Peak bytes measure the complete plain JSON continuation, without transport encoding or signatures.", "",
             "| Distribution / policy | Members | Rows | Requests / full pages | Peak ID / span entries | Peak ID / span bytes |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for result in results:
        if result["kind"] != "long":
            continue
        ids, spans = result["modes"]["ids"], result["modes"]["spans"]
        lines.append(f"| {result['name'].removeprefix('long_')} | {result['represented_members']:,} | "
                     f"{result['rows']:,} | {result['requests']} / {result['full_pages']} | "
                     f"{ids['peak_coverage_entries']:,} / {spans['peak_coverage_entries']:,} | "
                     f"{ids['peak_state_bytes']:,} / {spans['peak_state_bytes']:,} |")
    lines += ["", "## Interpretation", "",
              "1. A reserved hole splits a span. Joining the endpoints around that hole would silently claim an unserved member.",
              "2. A later grouped candidate may reach previously selected members above its cursor. Retirement follows all remaining group expansions as well as raw candidates and pending ownership decisions.",
              "3. Subtract permits separate rows for unreserved and reserved residual portions of one group. Defer skips the earlier row when any eligible residual member is reserved for another applicable owner. These are different output policies; the storage comparison holds the policy fixed.",
              "4. Fixed spans retire whole when no remaining consumer needs any represented member. They can keep harmless already-covered members longer than individual IDs. Descriptor gaps are never filled by inference.",
              "5. Whole groups and contiguous portions compress well; alternating holes can eliminate the benefit and tagged singleton descriptors can use more bytes than plain IDs. No universal compression ratio or constant token-size claim follows.",
              "6. Owner completion still applies release/omit to unserved claims. Consuming an owner candidate whose row was deferred does not itself serve the reservation. Group representability also does not guarantee service if its only candidate was already covered; no rewind is performed.", "",
              "## Reproduction and limits", "",
              "See `reference/grouped/README.md` in the source checkout for commands, assumptions and scoped exclusions. `comparison.json` includes per-plan results, schedules, source hashes, peak request indexes and retirement counts. `long_pages.csv` shows every long response for both representations.", "",
              "Retirement counters count descriptors disappearing between returned boundaries; they do not count descriptors created and retired wholly inside one request. Peaks are likewise observed response-state peaks, not RAM maxima.", "",
              "The generator is deterministic and varied, not an exhaustive enumeration of group models. Supplied static episodes, immutable membership, raw prefix sources and trusted logical continuation are assumed. No prepared buffers, automatic horizon discovery, live updates, authentication, actual database queries or hydration are validated here.", ""]
    (destination / "FINDINGS.md").write_text("\n".join(lines), encoding="utf-8")
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generated-cases", type=int, default=600)
    parser.add_argument("--groups", type=int, default=200)
    parser.add_argument("--members", type=int, default=12)
    parser.add_argument("--seed", type=int, default=20261119)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.generated_cases < 0:
        parser.error("--generated-cases must be nonnegative")
    results = run(args.generated_cases, args.groups, args.members, args.seed)
    totals = write_results(results, args.output, {
        "generated_cases": args.generated_cases, "groups": args.groups,
        "members": args.members, "seed": args.seed, "schedules": SCHEDULES,
    })
    print(json.dumps(totals, indent=2), flush=True)


if __name__ == "__main__":
    main()
