"""Render saved hybrid evidence without rerunning or rewriting measurements."""

import json
from pathlib import Path

from .generated import distribution_scenarios


MODES = ("ids", "spans", "hybrid")


def _json_bytes(value):
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8"))


def _peak_cell(metrics, kind):
    prefix = f"peak_{kind}_bytes"
    return (f"{metrics[prefix]:,} ({metrics[prefix + '_page']} / "
            f"{metrics[prefix + '_rows']:,} / {metrics[prefix + '_full_pages']})")


def zero_row_pending_costs(report):
    """Recover only provable first-request pending lists from matching fixed plans.

    The old report stored counts, not serialized pending bytes. At request one,
    before any row or owner completion, every initial claim is still pending.
    Validate the plan fingerprint, frontier and counts, then serialize the actual
    distinct IDs. Never estimate bytes from ID counts or a presumed ID format.
    """
    candidates = []
    for result in report["results"]:
        if result["kind"] != "long":
            continue
        peak_modes = [mode for mode in MODES
                      if result["modes"][mode]["peak_state_bytes_page"] == 1
                      and result["modes"][mode]["peak_state_bytes_rows"] == 0]
        if peak_modes:
            candidates.append((result, peak_modes))
    if not candidates:
        return []
    config = report.get("config", {})
    if not all(key in config for key in ("groups", "members", "seed")):
        return []
    plans = {case.name: case.plan for case in distribution_scenarios(
        config["groups"], config["members"], config["seed"])}
    recovered = []
    for result, peak_modes in candidates:
        plan = plans.get(result["name"])
        if plan is None or plan.fingerprint != result["plan_fingerprint"]:
            continue
        page = next((value for value in result.get("pages", ()) if value["page"] == 1), None)
        if page is None or page["rows_so_far"] != 0 or page["next_stage"] != 0:
            continue
        owner_indices = {stage.name: index for index, stage in enumerate(plan.stages)}
        if any(owner_indices[reservation.owner] == 0 for reservation in plan.reservations):
            continue
        pending = sorted({identity for reservation in plan.reservations for identity in reservation.item_ids})
        if any(page[mode]["pending_members"] != len(pending)
               or page[mode]["coverage_members"] != 0 or page[mode]["omitted_members"] != 0
               for mode in MODES):
            continue
        recovered.append({"name": result["name"], "page": page,
                          "peak_modes": tuple(peak_modes), "pending_members": len(pending),
                          "pending_bytes": _json_bytes(pending)})
    return recovered


def render_findings(report, *, reporting_revision=False):
    """Return prose from existing result fields; leave the supplied report intact."""
    totals, results = report["totals"], report["results"]
    previous_matches = totals["previous_two_mode_comparisons_unchanged"]
    long = [result for result in results if result["kind"] == "long"]
    lines = ["# Hybrid coverage comparison", "",
             "Hybrid selects bare IDs or a fixed span for each maximal consecutive selected run within one group, using exact compact JSON byte costs. A span wins only when strictly smaller; ties keep IDs. Selection and reservation policies are identical across all three modes.", ""]
    if reporting_revision:
        lines += ["Reporting revision: selected-coverage measurements now lead, and complete-continuation capacity is reported separately. This prose was regenerated from the saved report; its measurements, recording timestamp, source hashes, JSON and CSV evidence are unchanged.", ""]
    oracle_status = "All oracle prefixes and full results matched" if totals["all_oracles_match"] else "Some oracle comparisons failed"
    lines += [
        f"- {totals['three_way_comparisons']:,} three-way comparisons / {totals['individual_traversals']:,} traversals.",
        f"- {totals['audited_boundaries']:,} independently audited boundaries and {totals['replay_calls']:,} identical fresh-pager retries.",
        f"- {oracle_status}; {previous_matches:,} preceding two-mode comparisons reproduced unchanged.",
        "- Preceding comparisons are optional and supplied explicitly. When provided, matching results are checked and the input file hash is recorded without changing that file. Source hashes are recorded in `comparison.json`.", "",
        "## Selected coverage: page-50 comparison", "",
        "Compare the selected-coverage field first: pending reservations, deliberate omissions, vector metadata and representation labels are excluded. These are compact JSON bytes, before transport encoding or signatures.",
        "Page and quota sizes count rows, not represented members. Requests include empty work-limited responses; full pages contain 50 rows."]
    populations = sorted({result["scoped_records"] for result in long})
    if populations:
        lines.append("Scoped members per distribution: " + ", ".join(f"{count:,}" for count in populations) + ".")
    lines += ["", "Each peak cell is **bytes (request / cumulative selected rows / full pages)**. Each representation's peak is measured independently; different peaks need not share a request.", "",
              "| Distribution / policy | Rows | Requests / full pages | IDs coverage peak | Spans coverage peak | Hybrid coverage peak |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for result in long:
        peaks = " | ".join(_peak_cell(result["modes"][mode], "coverage") for mode in MODES)
        lines.append(f"| {result['name'].removeprefix('long_')} | {result['rows']:,} | "
                     f"{result['requests']} / {result['full_pages']} | {peaks} |")
    lines += ["", "| Distribution / policy | Peak hybrid span entries | Peak hybrid ID entries |",
              "| --- | ---: | ---: |"]
    for result in long:
        hybrid = result["modes"]["hybrid"]
        lines.append(f"| {result['name'].removeprefix('long_')} | {hybrid['peak_span_entries']:,} | {hybrid['peak_id_entries']:,} |")
    lines += ["", "Span and ID entry peaks are measured independently and need not occur on the same request. Full per-request counts are in `pages.csv`.", "",
              "## Complete-continuation capacity", "",
              "These separate peaks include selected coverage, explicit pending and omitted reservation IDs, and all vector metadata. They describe response-state capacity, not selected-coverage compression. Hydration, transport encoding and signatures are excluded. The `hybrid` representation label adds three bytes compared with `ids` when all other fields match.", "",
              "Each cell remains **bytes (request / cumulative selected rows / full pages)**; these are complete-state peaks, not the coverage peaks above.", "",
              "| Distribution / policy | IDs state peak | Spans state peak | Hybrid state peak |",
              "| --- | ---: | ---: | ---: |"]
    for result in long:
        peaks = " | ".join(_peak_cell(result["modes"][mode], "state") for mode in MODES)
        lines.append(f"| {result['name'].removeprefix('long_')} | {peaks} |")
    costs = zero_row_pending_costs(report)
    if costs:
        lines += ["", "### Zero-row peaks explained at the same boundary", "",
                  "At these first-request boundaries, the listed modes reach their complete-state peaks before any row is selected. The empty coverage list costs only two bytes. Pending reservation IDs dominate the response size. Every number in a row below comes from that same request; totals for other modes are not their independent peaks.", "",
                  "| Distribution / policy | Peak modes | Request / rows / full pages | Coverage bytes: IDs / spans / hybrid | Pending IDs | Pending-list bytes | Same-request state bytes: IDs / spans / hybrid |",
                  "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
        for recovered in costs:
            page = recovered["page"]
            coverage = " / ".join(f"{page[mode]['coverage_bytes']:,}" for mode in MODES)
            state = " / ".join(f"{page[mode]['state_bytes']:,}" for mode in MODES)
            peak_modes = " / ".join(recovered["peak_modes"])
            lines.append(f"| {recovered['name'].removeprefix('long_')} | {peak_modes} | {page['page']} / "
                         f"{page['rows_so_far']} / {page['full_pages_so_far']} | {coverage} | "
                         f"{recovered['pending_members']:,} | {recovered['pending_bytes']:,} | {state} |")
        lines += ["", "The original JSON stored pending counts but not pending-list bytes. The latter are reconstructed from the exact IDs in each fingerprint-matched fixed plan: no row has been selected and no owner has completed at this boundary, so every initial claim remains pending. The actual sorted ID list is serialized with compact, ASCII-escaped JSON; no per-ID size estimate is used."]
    lines += ["", "## What the hybrid does and does not guarantee", "",
              "- Every admission choice is no larger in coverage bytes than either all IDs or one span for that run. Singleton IDs carry no extra tag in the hybrid. Reserved and other same-group holes are never crossed.",
              "- The choice is fixed at admission. Fixed spans retain their original bounds while any member is still needed; IDs retire individually. A span initially cheaper than IDs can therefore be larger later. The dedicated lifecycle case tests this explicitly.",
              "- This is a choice per maximal run, not a globally optimal partition of arbitrary variable-length IDs and bounds. It does not repack across selected rows or predict future cursor progression.",
              "- Byte cost is not descriptor count or query cost. A byte-cheaper ID list can exceed an entry-count cap where a span fits; exact capacity failure remains explicit and retryable.",
              "- Reachability remains conservative: all potential future group expansions and pending owner decisions count. No minimum retained-state or query-speed claim is made.", "",
              f"Across the whole finite matrix, hybrid coverage exceeded IDs at {totals['hybrid_larger_coverage_boundaries']['ids']:,} boundaries and spans at {totals['hybrid_larger_coverage_boundaries']['spans']:,} boundaries. These counts exclude label overhead and include the deliberately adversarial lifecycle case. See per-plan examples in the JSON report.", "",
              "## Reproduce and next step", "",
              "Run `python -S -m reference.grouped.run_hybrid_checks` from the repository root for a new comparison. Dimensions and seed are configurable; output defaults to `reference/results/grouped/hybrid/`. To regenerate only prose from saved JSON, pass `--render-only --output <saved-directory>`; this does not rerun the model or rewrite JSON/CSV evidence. See `reference/grouped/README.md` in the checkout for test commands and scope.", "",
              "The comparison covers only these representations on the tested fixed-data/raw-source subset. Real engine queries, index plans, lookup cost and latency require separate validation. Prepared-source grouping, changing data, dynamic horizons and production transport remain separate work.", ""]
    return "\n".join(lines)


def regenerate_findings(destination):
    """Update only FINDINGS.md from the saved comparison.json at destination."""
    destination = Path(destination)
    report = json.loads((destination / "comparison.json").read_text(encoding="utf-8"))
    target = destination / "FINDINGS.md"
    target.write_text(render_findings(report, reporting_revision=True), encoding="utf-8")
    return target
