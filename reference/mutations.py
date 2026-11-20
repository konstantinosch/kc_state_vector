"""Bounded, named fault checks for the core/reservations reference.

Run ``python -S -m reference.mutations`` from the checkout root. Each variant
changes one model fragment in a fresh temporary copy and runs a focused existing
semantic test. A clean control must pass first. Only assertion failures count as
detection; crashes, import errors, timeouts and invalid edits fail the tooling.

These ten deliberately wrong implementations are our reproducible regression
catalogue, not a reproduction of another reviewer's unpublished mutation scripts
or a claim that every possible bug has been tested.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@dataclass(frozen=True)
class Mutation:
    name: str
    description: str
    before: str
    after: str
    tests: tuple[str, ...]


REFERENCE = "reference.tests.test_reference.ReferenceTests."
RETIREMENT = "reference.tests.test_retirement.RetirementAuditTests."
MUTATIONS = (
    Mutation(
        "ignore_selected_coverage", "Allow an already selected ID to be selected again.",
        'excluded = item.id in vector["coverage"] or item.id in vector["omitted"]',
        'excluded = item.id in vector["omitted"]',
        (REFERENCE + "test_prepared_source_carries_fetched_tail_and_refills_after_skips",)),
    Mutation(
        "charge_skips_to_quota", "Spend a selected-row quota on excluded or reserved candidates.",
        '                if not excluded and not reserved:\n'
        '                    rows.append((stage.name, item.id))\n'
        '                    vector["selected"][index] += 1',
        '                vector["selected"][index] += 1\n'
        '                if not excluded and not reserved:\n'
        '                    rows.append((stage.name, item.id))',
        (REFERENCE + "test_core_stage_exclusion_and_quotas",)),
    Mutation(
        "forget_zero_quota_owner_dependency", "Retire coverage still needed to resolve a pending owner.",
        '            if identity in vector["pending"] and self.plan._claims[identity].owner == index:\n'
        '                return True',
        '            if False:\n'
        '                return True',
        (RETIREMENT + "test_zero_quota_owner_keeps_covered_resolution_dependency",)),
    Mutation(
        "yielding_candidate_serves_owner", "Resolve ownership when a yielding stage merely consumes an ID.",
        '                if pending and claim.owner == index:',
        '                if pending:',
        (RETIREMENT + "test_earlier_cursor_cannot_discard_pending_ownership",)),
    Mutation(
        "invert_release_and_omit", "Omit released claims and release omitted claims at owner completion.",
        'elif identity not in vector["coverage"] and claim.on_unserved == "omit":',
        'elif identity not in vector["coverage"] and claim.on_unserved == "release":',
        (RETIREMENT + "test_omission_and_release_have_distinct_retained_sets",)),
    Mutation(
        "ignore_yield_set", "Block unaffected stages as if every stage yielded to the owner.",
        'reserved = pending and claim.owner != index and index in claim.yielding',
        'reserved = pending and claim.owner != index',
        (REFERENCE + "test_unaffected_earlier_stage_covers_owner_without_spending_owner_quota",)),
    Mutation(
        "retire_at_fetch_cursor", "Use fetched progress instead of consumed progress for retirement.",
        '            cursor = vector["cursors"][index]',
        '            cursor = (vector["buffers"][index]["fetch"]\n'
        '                      if vector["buffers"][index] is not None else vector["cursors"][index])',
        (RETIREMENT + "test_required_coverage_cannot_retire_at_fetch_cursor",)),
    Mutation(
        "ignore_state_binding", "Accept a continuation under a different plan or scope.",
        'if (vector["plan"], vector["scope"]) != (self.plan.fingerprint, self.plan.scope):',
        'if False:',
        (REFERENCE + "test_state_binding_rejects_changed_plan_and_scope",)),
    Mutation(
        "prune_before_decide", "Advance and prune before deciding whether the current candidate is excluded.",
        '                claim = self.plan._claims.get(item.id)\n'
        '                pending = item.id in vector["pending"]\n'
        '                excluded = item.id in vector["coverage"] or item.id in vector["omitted"]\n'
        '                reserved = pending and claim.owner != index and index in claim.yielding\n'
        '                # Decide first; advancing the cursor alone does not make a duplicate safe.\n'
        '                vector["cursors"][index] = list(item.position)',
        '                vector["cursors"][index] = list(item.position)\n'
        '                self._prune(vector)\n'
        '                claim = self.plan._claims.get(item.id)\n'
        '                pending = item.id in vector["pending"]\n'
        '                excluded = item.id in vector["coverage"] or item.id in vector["omitted"]\n'
        '                reserved = pending and claim.owner != index and index in claim.yielding',
        (REFERENCE + "test_empty_page_advances_and_has_a_successor",)),
    Mutation(
        "cursor_past_unread_batch", "Advance a raw cursor past the unread tail of a partially consumed batch.",
        '            if vector["stage"] == index and exhausted and processed == len(items):',
        '            if vector["stage"] == index and buffer is None and processed < len(items):\n'
        '                vector["cursors"][index] = list(items[-1].position)\n'
        '            if vector["stage"] == index and exhausted and processed == len(items):',
        (REFERENCE + "test_ordinary_source_refetches_unconsumed_tail",)),
)


def apply_mutation(source: str, mutation: Mutation) -> str:
    count = source.count(mutation.before)
    if count != 1:
        raise ValueError(f"{mutation.name}: expected one source match, found {count}")
    if mutation.before == mutation.after:
        raise ValueError(f"{mutation.name}: edit does not change source")
    changed = source.replace(mutation.before, mutation.after, 1)
    compile(changed, "mutated model.py", "exec")
    return changed


def classify(report: dict, expected_count: int, *, mutant: bool) -> str:
    """An assertion detects a behavioral fault; other failures are inconclusive."""
    if type(report) is not dict:
        return "infrastructure_error"
    fields = ("tests_run", "failures", "errors", "skipped", "expected_failures", "unexpected_successes")
    if any(type(report.get(key)) is not int or report[key] < 0 for key in fields):
        return "infrastructure_error"
    if (report["tests_run"] != expected_count or report["errors"] or report["skipped"]
            or report["expected_failures"] or report["unexpected_successes"]):
        return "infrastructure_error"
    if mutant:
        return "detected" if report["failures"] else "survived"
    return "baseline_failed" if report["failures"] else "passed"


def _copy_package(package: Path, destination: Path):
    """Copy Python sources only; no local research, generated evidence or caches."""
    for original in package.rglob("*.py"):
        relative = original.relative_to(package)
        if any(part in ("__pycache__", "results", "runs") for part in relative.parts):
            continue
        if not original.resolve().is_relative_to(package.resolve()):
            raise ValueError("Reference source resolves outside its package")
        target = destination / "reference" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)


def _worker(report_path: Path, tests: list[str]) -> int:
    suite = unittest.defaultTestLoader.loadTestsFromNames(tests)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    report = {
        "tests_run": result.testsRun, "failures": len(result.failures),
        "errors": len(result.errors), "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "output": stream.getvalue(),
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return 0


def _run_tests(root: Path, tests: tuple[str, ...], timeout: float, *, mutant: bool) -> dict:
    report_path = root / "result.json"
    environment = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME"):
        environment.pop(key, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [sys.executable, "-S", "-B", "-m", "reference.mutations",
               "--_worker-report", str(report_path), "--_tests", *tests]
    try:
        completed = subprocess.run(command, cwd=root, env=environment, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if completed.returncode or not report_path.is_file():
            return {"status": "infrastructure_error", "detail": completed.stderr or completed.stdout}
        report = json.loads(report_path.read_text(encoding="utf-8"))
        return {"status": classify(report, len(tests), mutant=mutant), **report}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "detail": f"Focused tests exceeded {timeout:g} seconds"}
    except (OSError, ValueError, TypeError) as exc:
        return {"status": "infrastructure_error", "detail": str(exc)}


def run_checks(*, package: Path | None = None, mutations=MUTATIONS, timeout=300) -> dict:
    package = Path(__file__).resolve().parent if package is None else Path(package)
    original = (package / "model.py").read_bytes()
    source = original.decode("utf-8")
    report = {"status": "failed", "model_sha256": hashlib.sha256(original).hexdigest(), "mutations": []}
    if not mutations or len({mutation.name for mutation in mutations}) != len(mutations):
        report["detail"] = "A nonempty catalogue with unique names is required"
        return report
    # Validate every edit before executing tests, so stale entries cannot be omitted.
    try:
        changes = {mutation.name: apply_mutation(source, mutation) for mutation in mutations}
    except (ValueError, SyntaxError) as exc:
        report["detail"] = str(exc)
        return report
    with tempfile.TemporaryDirectory(prefix="kc-reference-mutations-") as temporary:
        root = Path(temporary)
        baseline = root / "baseline"
        _copy_package(package, baseline)
        tests = tuple(dict.fromkeys(test for mutation in mutations for test in mutation.tests))
        report["baseline"] = _run_tests(baseline, tests, timeout, mutant=False)
        if report["baseline"]["status"] == "passed":
            for index, mutation in enumerate(mutations):
                work = root / f"variant-{index}"
                _copy_package(package, work)
                (work / "reference/model.py").write_text(changes[mutation.name], encoding="utf-8")
                outcome = _run_tests(work, mutation.tests, timeout, mutant=True)
                report["mutations"].append({"name": mutation.name, "tests": list(mutation.tests), **outcome})
    report["source_unchanged"] = (package / "model.py").read_bytes() == original
    if (report.get("baseline", {}).get("status") == "passed" and report["source_unchanged"]
            and len(report["mutations"]) == len(mutations)
            and all(item["status"] == "detected" for item in report["mutations"])):
        report["status"] = "passed"
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=300, help="Seconds per focused subprocess (default: 300)")
    parser.add_argument("--json", type=Path, help="Optional complete diagnostic report")
    parser.add_argument("--list", action="store_true", help="List the named variants without running them")
    parser.add_argument("--_worker-report", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_tests", nargs="+", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker_report is not None:
        if not args._tests:
            parser.error("Worker requires named tests")
        return _worker(args._worker_report, args._tests)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.json and args.json.resolve().is_relative_to(Path(__file__).resolve().parent):
        parser.error("--json must be outside the reference source package")
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}: {mutation.description}")
        return 0
    report = run_checks(timeout=args.timeout)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Baseline: {report.get('baseline', {}).get('status', 'not run')}")
    for item in report["mutations"]:
        print(f"{item['name']}: {item['status']}")
        if item["status"] != "detected":
            print(item.get("output", item.get("detail", "")))
    if report.get("detail"):
        print(report["detail"])
    if report.get("baseline", {}).get("status") not in (None, "passed"):
        print(report["baseline"].get("output", report["baseline"].get("detail", "")))
    print(f"Mutation checks: {report['status']} ({len(report['mutations'])} variants)")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
