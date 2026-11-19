"""Local correctness runs with 50-item pages and per-page state-retirement checks.

No database, payload hydration or query-latency measurements are involved. The
JSON and CSV reports describe logical continuation returned after each request;
they do not measure the larger temporary state inside a tentative page.
"""

import argparse
from collections import Counter
from dataclasses import dataclass
import csv
import json
from pathlib import Path

from .long_scenarios import make_scenarios
from .model import Pager
from .oracle import unpaged_oracle
from .retirement import audit_boundary


PAGE_SIZE = 50


@dataclass(frozen=True)
class Schedule:
    name: str
    batch_sizes: tuple[int, ...]
    candidate_budgets: tuple[int, ...]


SCHEDULES = (
    Schedule("wide", (173,), (1000,)),
    Schedule("tight", (37,), (80,)),
    Schedule("changing", (23, 127, 51), (31, 600, 73)),
)


def encode(state):
    return json.dumps(state, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def state_metrics(state):
    if state is None:
        return dict(coverage=0, pending=0, omitted=0, buffered=0,
                    exclusions=0, entries=0, state_bytes=0)
    counts = {name: len(state[name]) for name in ("coverage", "pending", "omitted")}
    counts["buffered"] = sum(len(value["items"]) for value in state["buffers"] if value is not None)
    counts["exclusions"] = counts["coverage"] + counts["omitted"]
    counts["entries"] = counts["exclusions"] + counts["pending"] + counts["buffered"]
    counts["state_bytes"] = len(encode(state).encode("utf-8"))
    return counts


def run_scenario(scenario, schedule):
    """Check every returned boundary and return reproducible counts, never timings."""
    plan = scenario.plan
    expected = unpaged_oracle(plan)
    pager = Pager(plan)
    state, history, pages, seen = None, [], [], set()
    streak = longest_streak = 0
    previous_sweeper_exclusions = None
    sweeper_comparisons = 0
    maximum_requests = sum(len(stage.items) for stage in plan.stages) + len(plan.stages) + 1
    for index in range(maximum_requests):
        batch = schedule.batch_sizes[index % len(schedule.batch_sizes)]
        budget = schedule.candidate_budgets[index % len(schedule.candidate_budgets)]
        before = encode(state)
        incoming = json.loads(before) if state is not None else None
        page = pager.page(incoming, page_size=PAGE_SIZE, batch_size=batch, candidate_budget=budget)
        context = f"{scenario.name}/{schedule.name}, page {index + 1}"
        assert encode(incoming) == before, f"{context}: mutated the supplied state"
        assert len(page.rows) <= PAGE_SIZE, f"{context}: page exceeded 50 items"
        assert len(page.rows) <= page.consumed <= budget, f"{context}: candidate budget violated"
        assert tuple(expected[len(history):len(history) + len(page.rows)]) == page.rows, f"{context}: oracle prefix mismatch"
        assert not seen.intersection(page.ids), f"{context}: duplicate ID across pages"
        assert len(set(page.ids)) == len(page.ids), f"{context}: duplicate ID within page"
        seen.update(page.ids)
        history.extend(page.rows)
        try:
            audit_boundary(plan, page.state, history)
        except AssertionError as exc:
            raise AssertionError(f"{context}: {exc}") from exc
        if page.state is not None:
            assert encode(page.state) != before, f"{context}: no continuation progress"
            if state is not None:
                assert page.state["stage"] >= state["stage"], f"{context}: stage moved backwards"
                for old, new in zip(state["cursors"], page.state["cursors"]):
                    assert old is None or (new is not None and tuple(new) <= tuple(old)), f"{context}: consumed cursor moved backwards"

        # Every scenario ends with an unlimited, non-owning source. Once there,
        # no selected ID needs a new exclusion; inherited exclusions only leave.
        in_sweeper = page.state is not None and page.state["stage"] == len(plan.stages) - 1
        if in_sweeper:
            assert not page.state["pending"], f"{context}: final sweeper still has pending ownership"
            exclusions = set(page.state["coverage"]) | set(page.state["omitted"])
            if previous_sweeper_exclusions is not None:
                assert exclusions <= previous_sweeper_exclusions, f"{context}: sweeper added exclusions"
                sweeper_comparisons += 1
            previous_sweeper_exclusions = exclusions
        elif page.state is None and previous_sweeper_exclusions is not None:
            sweeper_comparisons += 1

        streak = streak + 1 if len(page.rows) == PAGE_SIZE else 0
        longest_streak = max(longest_streak, streak)
        pages.append({
            "page": index + 1, "rows": len(page.rows), "selected_total": len(history),
            "consumed": page.consumed, "fetched": page.fetched, "reads": page.reads,
            "batch_size": batch, "candidate_budget": budget,
            "next_stage": plan.stages[page.state["stage"]].name if page.state is not None else "done",
            "in_sweeper": in_sweeper, **state_metrics(page.state),
        })
        if page.state is None:
            break
        state = json.loads(encode(page.state))
    else:
        raise AssertionError(f"{scenario.name}/{schedule.name}: finite traversal failed to terminate")
    assert history == expected, f"{scenario.name}/{schedule.name}: whole-result oracle mismatch"

    peak_entries = max(pages, key=lambda row: row["entries"])
    peak_bytes = max(pages, key=lambda row: row["state_bytes"])
    peak_exclusions = max(pages, key=lambda row: row["exclusions"])
    memberships = Counter(item.id for stage in plan.stages for item in stage.items)
    return {
        "scenario": scenario.name, "description": scenario.description, "schedule": schedule.name,
        "page_size": PAGE_SIZE, "unique_input_ids": len(memberships),
        "input_ids_in_multiple_sources": sum(value > 1 for value in memberships.values()),
        "source_sizes": {stage.name: len(stage.items) for stage in plan.stages},
        "stage_quotas": {stage.name: stage.quota for stage in plan.stages},
        "declared_reservation_ids": sum(len(claim.item_ids) for claim in plan.reservations),
        "unique_reserved_ids": len({identity for claim in plan.reservations for identity in claim.item_ids}),
        "selected_ids": len(history), "requests": len(pages),
        "full_pages": sum(row["rows"] == PAGE_SIZE for row in pages),
        "empty_pages": sum(row["rows"] == 0 for row in pages),
        "longest_full_page_streak": longest_streak,
        "peak_returned_entries": peak_entries["entries"], "peak_entries_page": peak_entries["page"],
        "peak_returned_bytes": peak_bytes["state_bytes"], "peak_bytes_page": peak_bytes["page"],
        "peak_returned_exclusions": peak_exclusions["exclusions"],
        "peak_exclusions_page": peak_exclusions["page"],
        "sweeper_retirement_comparisons": sweeper_comparisons,
        "oracle_match": True, "all_boundaries_audited": True, "pages": pages,
    }


def assert_long_result(result):
    """Ensure a default-size fixture actually exercises long 50-row paging."""
    label = f"{result['scenario']}/{result['schedule']}"
    assert result["selected_ids"] >= 1000, f"{label}: fewer than 1,000 selected IDs"
    assert result["full_pages"] >= 10, f"{label}: too few full 50-item pages"
    assert result["longest_full_page_streak"] >= 2, f"{label}: no successive full pages"
    assert result["sweeper_retirement_comparisons"] >= 10, f"{label}: sweeper traversal too short"
    assert result["pages"][-1]["entries"] == 0, f"{label}: terminal retained entries"
    assert result["pages"][-1]["state_bytes"] == 0, f"{label}: terminal continuation bytes"


def write_reports(output, results, *, size, seed):
    output.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": 1, "page_size": PAGE_SIZE, "size_parameter": size, "seed": seed,
              "scope": "Fixed-data ID-only correctness; returned continuation size, not engine latency or transient peak memory.",
              "traversals": results}
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    with (output / "pages.csv").open("w", encoding="utf-8", newline="") as stream:
        fields = ["scenario", "schedule", *results[0]["pages"][0]]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for result in results:
            for page in result["pages"]:
                writer.writerow({"scenario": result["scenario"], "schedule": result["schedule"], **page})
    lines = ["# Local long-traversal results", "",
             f"{len(results)} complete traversals; page size 50; size parameter {size}; seed {seed}.", "",
             "Every page is checked against an independent ordered-result oracle and a separate state-retirement auditor.",
             "Metrics describe returned continuation, after pruning. They exclude tentative in-request peaks, source data, transport envelopes and hydration.",
             "No database or search service runs here, and no query-latency claim is made.", "",
             "| Scenario | Schedule | IDs selected | Requests | Full / empty pages | Peak returned entries (page) | Peak bytes (page) |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for result in results:
        lines.append(f"| {result['scenario']} | {result['schedule']} | {result['selected_ids']} | {result['requests']} | "
                     f"{result['full_pages']} / {result['empty_pages']} | {result['peak_returned_entries']} ({result['peak_entries_page']}) | "
                     f"{result['peak_returned_bytes']} ({result['peak_bytes_page']}) |")
    lines.extend(["", "## Distributions", ""])
    for name, description in dict((r["scenario"], r["description"]) for r in results).items():
        lines.append(f"- **{name}:** {description}")
    lines.extend(["", "The final non-owning sweeper is checked for set-wise exclusion shrinkage at every returned boundary.",
                  "Logical continuation ends at zero entries/bytes when no continuation is needed; this does not claim that total RAM use reaches zero.",
                  "`pages.csv` records each page's coverage, pending reservations, deliberate omissions, buffers, bytes and work counters.", ""])
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=20261119)
    parser.add_argument("--scenario", help="run one named distribution")
    parser.add_argument("--schedule", choices=[value.name for value in SCHEDULES], help="run one work schedule")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "results" / "long_traversals")
    args = parser.parse_args()
    scenarios = [value for value in make_scenarios(args.size, args.seed) if args.scenario is None or value.name == args.scenario]
    if not scenarios:
        parser.error("Unknown scenario")
    schedules = [value for value in SCHEDULES if args.schedule is None or value.name == args.schedule]
    results = []
    for scenario in scenarios:
        for schedule in schedules:
            result = run_scenario(scenario, schedule)
            if args.size >= 3000:
                assert_long_result(result)
            results.append(result)
            print(f"PASS {scenario.name}/{schedule.name}: {result['selected_ids']} IDs, "
                  f"{result['requests']} requests, {result['full_pages']} full pages; "
                  f"peak returned state {result['peak_returned_entries']} entries at page {result['peak_entries_page']}", flush=True)
    write_reports(args.output, results, size=args.size, seed=args.seed)
    print(f"Reports: {args.output.resolve()}")


if __name__ == "__main__":
    main()
