"""Named semantic regression checks for the bundled sample and tiered checker.

Only wrong selected rows count as a detection. Every fault/tier has a healthy control.
"""
import argparse
import ast
import hashlib
import json
import math
from pathlib import Path
import tempfile

from check_pager import load_fixture, run_case

HERE = Path(__file__).resolve().parent


def schedule(name, pages, batches, budgets):
    return {"name": name, "page_sizes": pages, "batch_sizes": batches, "candidate_budgets": budgets}


MUTATIONS = (
    ("prune_before_decide", "empty_duplicate_page",
     "                excluded = identity in state['coverage']\n                state['cursors'][index] = [sort_key, identity]",
     "                state['cursors'][index] = [sort_key, identity]\n                prune(state)\n                excluded = identity in state['coverage']",
     schedule("duplicate-witness", [2], [3], [2,1,1,1])),
    ("cursor_past_unread_batch", "unread_batch_tail",
     "            if state['stage'] == index and processed == len(batch) and len(batch) == len(remaining):",
     "            if state['stage'] == index and processed < len(batch):\n                state['cursors'][index] = list(batch[-1])\n"
     "            if state['stage'] == index and processed == len(batch) and len(batch) == len(remaining):",
     schedule("unread-tail-witness", [1], [3], [50])),
    ("blanket_category_exclusion", "priority_51_49_48",
     "            state['stage'] += 1\n\n    def prune",
     "            state['coverage'] = sorted(set(state['coverage']) | memberships[index])\n"
     "            state['stage'] += 1\n\n    def prune",
     schedule("priority-witness", [3], [3], [50])),
)


def is_detected(result):
    return (result.get("status") == "failed" and result.get("exit_code") == 1
            and result.get("failure", {}).get("kind") in {"row_mismatch", "early_exhaustion", "extra_row"})


def apply_mutation(source, before, after):
    if source.count(before) != 1 or before == after:
        raise ValueError("Mutation must change exactly one source fragment")
    changed = source.replace(before, after)
    ast.parse(changed)
    return changed


BASE_WRAPPER = '''import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("candidate", Path(__file__).with_name("candidate.py"))
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)
CAPABILITIES = set()
def make_pager(fixture):
    implementation = candidate.make_pager(fixture)
    def page(continuation, *, page_size):
        value = implementation(continuation, page_size=page_size, batch_size=3, candidate_budget=50)
        return {"rows": [row["id"] for row in value["rows"]], "continuation": value["continuation"]}
    return page
'''


def run_checks(timeout=300):
    report = {"status": "failed", "controls": [], "mutations": []}
    source_path = HERE / "sample_pager.py"
    original = source_path.read_bytes()
    report["sample_sha256"] = hashlib.sha256(original).hexdigest()
    try:
        if type(timeout) not in (float, int) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be positive and finite")
        source = original.decode("utf-8")
        variants = [(name, fixture, apply_mutation(source, before, after), chosen)
                    for name, fixture, before, after, chosen in MUTATIONS]
        with tempfile.TemporaryDirectory(prefix="kc-skill-mutations-") as folder:
            root = Path(folder)
            for name, fixture_name, changed, chosen in variants:
                fixture = load_fixture(HERE / "fixtures" / f"{fixture_name}.json")
                for tier in ("base", "full"):
                    work = root / (name + "-" + tier)
                    work.mkdir()
                    candidate = work / "candidate.py"
                    candidate.write_text(source, encoding="utf-8", newline="\n")
                    adapter = candidate
                    if tier == "base":
                        adapter = work / "adapter.py"
                        adapter.write_text(BASE_WRAPPER, encoding="utf-8", newline="\n")
                    control = run_case(adapter, fixture, chosen, timeout=timeout)
                    report["controls"].append({"name": name, "tier": tier, "result": control})
                    if control["status"] != "passed":
                        raise ValueError("Healthy control failed; no semantic detection can be claimed")
                    candidate.write_text(changed, encoding="utf-8", newline="\n")
                    result = run_case(adapter, fixture, chosen, timeout=timeout)
                    report["mutations"].append({"name": name, "tier": tier, "fixture": fixture_name,
                                                "schedule": chosen, "detected": is_detected(result), "result": result})
        if len(report["mutations"]) == 2 * len(MUTATIONS) and all(r["detected"] for r in report["mutations"]):
            report["status"] = "passed"
    except (OSError, TypeError, ValueError, SyntaxError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["source_unchanged"] = source_path.read_bytes() == original
    if not report["source_unchanged"]:
        report["status"] = "failed"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive and finite")
    if args.json and (args.json.exists() or args.json.resolve().is_relative_to(HERE.parent)):
        parser.error("--json must be a new report outside the skill package")
    report = run_checks(args.timeout)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        with args.json.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    for entry in report["mutations"]:
        print(f"{entry['name']} [{entry['tier']}]: " + ("detected" if entry["detected"] else "NOT DETECTED"))
    if "error" in report:
        print(report["error"])
    print("Skill mutation checks:", report["status"])
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
