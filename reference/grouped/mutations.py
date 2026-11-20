"""Named semantic fault checks for grouped coverage and partial reservations.

Run ``python -S -m reference.grouped.mutations``. Each valid one-fragment edit
is made in a fresh temporary copy after healthy focused tests pass. Only failed
semantic assertions detect faults; syntax errors, crashes, skips and timeouts
fail the tooling. Provenance/hash tests never count as detections.

This is a bounded, independently defined catalogue, not a reproduction of a
reviewer's unpublished patches or an exhaustive enumeration of possible bugs.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import tempfile

from reference.mutations import apply_mutation, _copy_package, _run_tests


@dataclass(frozen=True)
class Mutation:
    name: str
    description: str
    before: str
    after: str
    tests: tuple[str, ...]
    target: str = 'model.py'


PAGER = 'reference.grouped.tests.test_pager.PagerTests.'
CONTRACT = 'reference.grouped.tests.test_contract.ContractTests.'
CODEC = 'reference.grouped.tests.test_coverage.CoverageCodecTests.'
HYBRID = 'reference.grouped.tests.test_hybrid_coverage.HybridCoverageTests.'
REGRESSION = 'reference.grouped.tests.test_mutations.GroupedMutationRegressions.'
MUTATIONS = (
    Mutation(
        'retire_by_raw_position', 'Forget members that a future candidate can still expand.',
        '                    needed.update(members)',
        '                    needed.update(identity for identity in members\n'
        '                                  if cursor is None or self.plan._records[identity].position < tuple(cursor))',
        (PAGER + 'test_cursor_crossing_does_not_retire_coverage_needed_by_group_expansion',)),
    Mutation(
        'span_crosses_reserved_hole', 'Join selected runs across an unrepresented same-group member.',
        '            else:\n                emit(group, run)\n                run = []',
        '            else:\n                pass',
        (CODEC + 'test_reserved_middle_member_splits_coverage',), target='coverage.py'),
    Mutation(
        'defer_policy_ignored', 'Select a partial grouped row even when the policy requires deferral.',
        '                    if not (stage.mode == "grouped" and stage.partial_policy == "defer" and reserved):',
        '                    if True:',
        (PAGER + 'test_consumed_deferred_owner_keeps_pending_claims_until_completion',)),
    Mutation(
        'consumed_owner_candidate_is_served', 'Resolve an owner claim whose grouped row was deferred.',
        '                # A deferred owner\'s raw candidate is NOT a served reservation.\n'
        '                vector["pending"] = sorted(pending)',
        '                if item.id in pending and self.plan._claims[item.id][0] == index:\n'
        '                    pending.remove(item.id)\n'
        '                vector["pending"] = sorted(pending)',
        (PAGER + 'test_consumed_deferred_owner_keeps_pending_claims_until_completion',)),
    Mutation(
        'forget_pending_dependencies', 'Retire coverage still needed to resolve a zero-quota owner.',
        '        needed = set(state["pending"])',
        '        needed = set()',
        (REGRESSION + 'test_pending_zero_quota_owner_retains_covered_member',)),
    Mutation(
        'prune_before_deciding', 'Advance and prune before testing the current candidate for prior coverage.',
        '                covered = self._covered(vector)',
        '                vector["cursors"][index] = list(item.position)\n'
        '                self._prune(vector)\n'
        '                covered = self._covered(vector)',
        (CONTRACT + 'test_fixed_span_survives_until_last_future_group_candidate',)),
    Mutation(
        'ignore_selective_yield', 'Block stages that were not declared to yield to the owner.',
        '                    return identity in pending and claim[0] != index and index in claim[1]',
        '                    return identity in pending and claim[0] != index',
        (REGRESSION + 'test_unaffected_stage_can_represent_reserved_member',)),
    Mutation(
        'group_expansion_ignores_coverage', 'Represent already-selected members through a new candidate.',
        '                    available = tuple(identity for identity in represented if identity not in covered and identity not in omitted)',
        '                    available = tuple(identity for identity in represented if identity not in omitted)',
        (PAGER + 'test_cursor_crossing_does_not_retire_coverage_needed_by_group_expansion',)),
    Mutation(
        'owner_omits_already_covered_member', 'Turn an already represented owner claim into an omission.',
        '            if identity not in covered and self.plan._claims[identity][2] == "omit"})',
        '            if self.plan._claims[identity][2] == "omit"})',
        (REGRESSION + 'test_owner_completion_does_not_omit_covered_member',)),
    Mutation(
        'hybrid_tie_prefers_span', 'Choose a span without a strict serialized-byte saving.',
        '            if mode == "hybrid" and _json_bytes([span]) >= _json_bytes(explicit):',
        '            if mode == "hybrid" and _json_bytes([span]) > _json_bytes(explicit):',
        (HYBRID + 'test_tie_keeps_ids_and_one_byte_saving_selects_span',), target='coverage.py'),
)


def run_checks(*, package=None, mutations=MUTATIONS, timeout=300):
    """Check a public reference package; shared worker utilities enforce controls."""
    package = Path(__file__).resolve().parents[1] if package is None else Path(package)
    grouped = package / 'grouped'
    report = {'status': 'failed', 'mutations': []}
    if (not mutations or len({m.name for m in mutations}) != len(mutations)
            or any(m.target not in ('model.py', 'coverage.py') for m in mutations)):
        report['detail'] = 'A nonempty, uniquely named catalogue of model/coverage edits is required'
        return report
    originals = {name: (grouped / name).read_bytes() for name in {m.target for m in mutations}}
    report['source_sha256'] = {name: hashlib.sha256(data).hexdigest()
                              for name, data in sorted(originals.items())}
    try:
        changes = {m.name: apply_mutation(originals[m.target].decode('utf-8'), m) for m in mutations}
    except (ValueError, SyntaxError) as exc:
        report['detail'] = str(exc)
        return report
    with tempfile.TemporaryDirectory(prefix='kc-grouped-mutations-') as temporary:
        root = Path(temporary)
        baseline = root / 'baseline'
        _copy_package(package, baseline)
        tests = tuple(dict.fromkeys(test for m in mutations for test in m.tests))
        report['baseline'] = _run_tests(baseline, tests, timeout, mutant=False)
        if report['baseline']['status'] == 'passed':
            for index, mutation in enumerate(mutations):
                work = root / f'variant-{index}'
                _copy_package(package, work)
                (work / 'reference/grouped' / mutation.target).write_text(
                    changes[mutation.name], encoding='utf-8')
                outcome = _run_tests(work, mutation.tests, timeout, mutant=True)
                report['mutations'].append({'name': mutation.name, 'target': mutation.target,
                                            'tests': list(mutation.tests), **outcome})
    report['source_unchanged'] = all((grouped / name).read_bytes() == data
                                   for name, data in originals.items())
    if (report.get('baseline', {}).get('status') == 'passed' and report['source_unchanged']
            and len(report['mutations']) == len(mutations)
            and all(value['status'] == 'detected' for value in report['mutations'])):
        report['status'] = 'passed'
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, default=300, help='Seconds per focused subprocess (default: 300)')
    parser.add_argument('--json', type=Path, help='Optional diagnostic file outside the reference package')
    parser.add_argument('--list', action='store_true', help='List faults without running them')
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout must be positive')
    if args.json and args.json.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error('--json must be outside the reference source package')
    if args.list:
        for mutation in MUTATIONS:
            print(f'{mutation.name}: {mutation.description}')
        return 0
    report = run_checks(timeout=args.timeout)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(f"Baseline: {report.get('baseline', {}).get('status', 'not run')}")
    for value in report['mutations']:
        print(f"{value['name']}: {value['status']}")
        if value['status'] != 'detected':
            print(value.get('output', value.get('detail', '')))
    if report.get('detail'):
        print(report['detail'])
    if report.get('baseline', {}).get('status') not in (None, 'passed'):
        print(report['baseline'].get('output', report['baseline'].get('detail', '')))
    print(f"Grouped mutation checks: {report['status']} ({len(report['mutations'])} variants)")
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
