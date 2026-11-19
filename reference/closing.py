"""Run the local closing failure/capacity pass and record its bounded evidence."""

import argparse
import hashlib
import json
from pathlib import Path

from .capacity_checks import run_capacity_checks
from .failure_checks import run_failure_checks


ROOT = Path(__file__).parent
OUTPUT = ROOT / "results" / "closing"
CHECKED_SOURCES = ("model.py", "oracle.py", "retirement.py", "failure_checks.py",
                   "capacity_checks.py", "closing.py")


def source_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in CHECKED_SOURCES}


def run_closing_checks(output=OUTPUT):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    summary_path = output / "summary.json"
    summary_path.write_text(json.dumps({"schema_version": 1, "status": "running"}) + "\n", encoding="utf-8")
    hashes = source_hashes()
    failures = run_failure_checks()
    capacity = run_capacity_checks()
    if not failures["passed"] or capacity["status"] != "passed":
        raise AssertionError("Closing checks did not report success")
    if source_hashes() != hashes:
        raise AssertionError("Checked source files changed during the closing run")
    summary = {"schema_version": 1, "status": "passed", "source_sha256": hashes,
               "read_failures": failures, "capacity": capacity}
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    lines = ["# Local closing failure/capacity pass", "",
             "All selected checks passed. No database, hydration or network delivery is involved.", "",
             "## Read failures and retries", "",
             f"{failures['fixture_count']} fixtures across two request schedules exercised {failures['read_positions']} actual read positions in {failures['baseline_requests']} healthy requests.",
             f"Every read was failed once with an exception and once with an incomplete response: **{failures['injected_failures']} injected failures**.",
             f"All preserved incoming continuation and returned no partial Page. {failures['fresh_source_retries']} fresh-source retries and {failures['same_source_retries']} recovered same-source/pager retries reproduced the exact healthy Page.",
             f"All {failures['complete_recovery_traversals']} recovered complete results matched the independent oracle; {failures['audited_recovery_boundaries']} recovery boundaries were audited.", "",
             "Witnesses cover reads after owner completion by quota, exhaustion or zero quota; release/omit; covered ownership; tentative selections; and prepared refills within and across requests. Witness categories overlap and count read positions, not additional failures.",
             "External read side effects are not rolled back. These are atomic function-result and logical-continuation checks, not exactly-once delivery guarantees.", "",
             "## Capacity", "",
             f"{capacity['fixtures']} fixtures across {capacity['schedules']} schedules checked {capacity['requests_checked']} healthy request boundaries and {capacity['checkpoints']} logical-state checkpoints.",
             f"**{capacity['exact_fit_checks']} exact-fit checks** cover entries, encoded bytes, and both dimensions together.",
             f"**{capacity['one_below_failures']} one-below failures** include repeated attempts from unchanged state; {capacity['healthy_retry_checks']} raised-capacity retries reproduce the healthy Page and correct eventual result.",
             f"An independent black-box search used {capacity['black_box_search_attempts']} attempts to confirm the minimum successful limit for each request and dimension.", "",
             "Limits apply at enforced logical-state checkpoints, not every intermediate assignment or absolute peak RAM. Entry accounting includes IDs in multiple roles; bytes use the model's compact ASCII-escaped JSON representation. Terminal exemptions and genuinely zero-entry cases are tested separately.", "",
             "### Prepared refill witness", "", "```json",
             json.dumps(capacity["witnesses"]["refill_exceeds_returned_size"], indent=2), "```", "",
             "This witness requires an incoming state that fits the returned-size cap, then an actual failure at prepared refill. It distinguishes the refill requirement from an immediate rejection of an already oversized incoming state.", "",
             "## Reproduce", "", "```console", "python -m reference.closing", "```", "",
             "The adjacent `summary.json` contains per-fixture records, witnesses and source hashes. The full local unittest suite separately checks that the injectors and report validators cannot silently pass when disabled or corrupted.", ""]
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"PASS: {failures['injected_failures']} injected read failures; "
          f"{capacity['exact_fit_checks']} exact-fit checks; {capacity['one_below_failures']} repeated one-below failures")
    print(f"Report: {(output / 'REPORT.md').resolve()}")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    run_closing_checks(args.output)


if __name__ == "__main__":
    main()
