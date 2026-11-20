"""Reproduce placement storage and retry costs using the actual fixed-data selectors.

The default core suite is bounded. The full suite adds the existing 2,400-member
grouped distributions, including the small-page cases where recomputation can
cost more storage. This runner contains no dynamic-horizon implementation.
"""

import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import csv
import hashlib
import json
from pathlib import Path

from reference.long_scenarios import make_scenarios
from reference.grouped.generated import distribution_scenarios as grouped_distributions

from .adapters import Adapter, parameters
from .protocol import Service as OneWay, Store as FullStore
from .two_way import Service as TwoWay
from .recompute import Service as Recompute, Store as RecomputeStore
from .recompute_checks import healthy, retry_and_faults, require
from .scenarios import Scenario, core_two_growth_cycles


ROOT = Path(__file__).resolve().parent
REFERENCE = ROOT.parent
MODES = ('one_way', 'two_way', 'recompute')
STRATEGIES = ((OneWay, FullStore), (TwoWay, FullStore), (Recompute, RecomputeStore))
PAIRS = (('one_way', 'two_way'), ('one_way', 'recompute'), ('two_way', 'recompute'))
SEED = 20261119
STORAGE_COMPONENTS = ('hot', 'backup', 'heads', 'redirects', 'vector', 'coordination',
                      'response_payload', 'head_metadata', 'total',
                      'return_bridge_payload', 'other_response_payload')
_FIXTURES = None


def fixtures(suite):
    """Reuse public fixtures; names, domains and dimensions are fixed evidence."""
    result = {}
    if suite in ('core', 'full', 'smoke'):
        size = 600
        case = next(c for c in make_scenarios(size, SEED) if c.name == 'core_growth_control')
        scenario = Scenario(f'{case.name}_{size}', Adapter(case.plan), case.description)
        result[scenario.name] = (scenario, 'core')
        if suite != 'smoke':
            scenario = core_two_growth_cycles()
            result[scenario.name] = (scenario, 'core')
    if suite in ('grouped', 'full'):
        for case in grouped_distributions(200, 12, SEED):
            for representation in ('ids', 'spans', 'hybrid'):
                scenario = Scenario(f'{case.name}_{representation}', Adapter(case.plan, representation),
                                    case.description)
                result[scenario.name] = (scenario, 'grouped')
    if not result:
        raise ValueError('Unknown or empty comparison suite: ' + str(suite))
    return result


def schedule(suite, cases=None):
    cases = fixtures(suite) if cases is None else cases
    result = []
    for name, (_, category) in sorted(cases.items()):
        thresholds = (2048,) if suite == 'smoke' else (2048, 4096, 8192)
        pages = (50,)
        backups = (True, False) if category == 'core' else (True,)
        for page in pages:
            for threshold in thresholds:
                for backup in backups:
                    result.append((name, page, threshold, backup))
        if category == 'grouped' and name.startswith('long_alternating_members_subtract_'):
            result.extend((name, page, 4096, True) for page in (1, 3, 7))
    return result


def _partition_payloads(control):
    """Count the retained return response separately, without double counting it."""
    for page in control['pages']:
        bridge = page['response_payload_bytes'] if page['transition'] == 'handle_to_inline' else 0
        page['return_bridge_payload_bytes'] = bridge
        page['other_response_payload_bytes'] = page['response_payload_bytes'] - bridge
        require(page['total_bytes'] == sum(page[name + '_bytes'] for name in
                    ('hot', 'backup', 'head_metadata', 'return_bridge_payload',
                     'other_response_payload', 'redirects')), 'storage partitions do not sum')
    for name in ('return_bridge_payload', 'other_response_payload'):
        control['summary'][name + '_byte_pages'] = sum(p[name + '_bytes'] for p in control['pages'])
        control['summary']['peak_' + name + '_bytes'] = max(p[name + '_bytes'] for p in control['pages'])


def compare_case(adapter, params, threshold, backup=True):
    """Compare three contracts on the same plan, chain, parameters and store policy.

    One-way's v1 envelope and the optional contracts' v2 envelope differ. Their
    logical selections must match, but their token lengths and switch points need
    not. The optional contracts use the same wire shape and must match placement.
    """
    params = parameters(params)
    expected = adapter.expected_rows()  # Independent oracle, never a selection source.
    controls, modes = {}, {}
    for mode, (service_type, store_type) in zip(MODES, STRATEGIES):
        control = healthy(service_type, store_type, adapter, params, expected, threshold, backup)
        # Run faults before adding report-only fields used by the strict boundary check.
        failures = retry_and_faults(service_type, store_type, adapter, params, threshold, backup, control)
        _partition_payloads(control)
        controls[mode] = control
        modes[mode] = {'summary': control['summary'], 'pages': control['pages'], 'failures': failures}
    base = controls['one_way']
    for mode in MODES[1:]:
        current = controls[mode]
        require(base['states'] == current['states'], 'placement changed selected logical states')
        require(len(base['responses']) == len(current['responses']), 'placement changed request count')
        for left, right in zip(base['responses'], current['responses']):
            for field in ('rows', 'work', 'status', 'client_action', 'chain', 'revision'):
                require(left[field] == right[field], 'placement changed result field: ' + field)
        require(base['summary']['selector'] == current['summary']['selector'],
                'placement changed healthy selector work')
    for left, right in zip(controls['two_way']['pages'], controls['recompute']['pages']):
        require(tuple(left[key] for key in ('input_kind', 'kind', 'token_bytes', 'vector_bytes')) ==
                tuple(right[key] for key in ('input_kind', 'kind', 'token_bytes', 'vector_bytes')),
                'replay policy changed optional-contract wire size, placement or vector storage')
    comparisons = {}
    for old, new in PAIRS:
        a, b = modes[old], modes[new]
        savings = {name + '_byte_pages': a['summary'][name + '_byte_pages'] -
                   b['summary'][name + '_byte_pages'] for name in STORAGE_COMPONENTS}
        savings['token_bytes'] = a['summary']['token_bytes'] - b['summary']['token_bytes']
        savings['vector_resident_pages'] = a['summary']['vector_resident_pages'] - b['summary']['vector_resident_pages']
        old_retry = a['failures']['selector']['phases'].get('retry', {})
        new_retry = b['failures']['selector']['phases'].get('retry', {})
        extra = {key: new_retry.get(key, 0) - old_retry.get(key, 0)
                 for key in ('select_calls', 'consumed', 'fetched', 'reads')}
        comparisons[old + '_to_' + new] = {'saved': savings, 'extra_retry': extra}
    return {'threshold_bytes': threshold, 'backup_enabled': backup, 'parameters': params,
            'plan_fingerprint': adapter.plan.fingerprint, 'binding': adapter.binding,
            'representation': adapter.representation, 'scoped_records': len(adapter.records),
            'oracle_rows': len(expected), 'strategies': modes, 'comparisons': comparisons,
            'actual_selector_used': True, 'logical_results_identical': True,
            'optional_wire_bytes_identical': True, 'all_checks_passed': True}


def _initialize(suite):
    global _FIXTURES
    _FIXTURES = fixtures(suite)


def _run_task(spec):
    name, page, threshold, backup = spec
    require(_FIXTURES is not None and name in _FIXTURES, 'worker fixture missing')
    scenario, category = _FIXTURES[name]
    result = compare_case(scenario.adapter,
                          {'page_size': page, 'batch_size': 50, 'candidate_budget': 1000}, threshold, backup)
    result.update(name=name, description=scenario.description, category=category, page_size=page)
    return result


def source_hashes():
    return {path.relative_to(REFERENCE.parent).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(REFERENCE.rglob('*.py'))
            if '__pycache__' not in path.parts and not path.is_relative_to(REFERENCE / 'results')}


def summarize_results(results):
    totals = {'cases': len(results), 'protocol_traversals': 6 * len(results),
              'all_checks_passed': all(case['all_checks_passed'] for case in results), 'strategies': {}}
    for mode in MODES:
        entries = [case['strategies'][mode] for case in results]
        pages = [page for entry in entries for page in entry['pages']]
        nonterminal = [page for page in pages if page['kind'] != 'terminal']
        inline_count = sum(page['kind'] == 'inline' for page in nonterminal)
        costs = defaultdict(lambda: defaultdict(int))
        faults = defaultdict(int)
        for entry in entries:
            for phase, values in entry['failures']['selector']['phases'].items():
                for key, value in values.items():
                    costs[phase][key] += value
            for key, value in entry['failures']['counts'].items():
                faults[key] += value
        totals['strategies'][mode] = {
            'healthy_requests': sum(e['summary']['requests'] for e in entries),
            'terminal_responses': len(pages) - len(nonterminal),
            'nonterminal_responses': len(nonterminal),
            'inline_nonterminal_responses': inline_count,
            'inline_nonterminal_fraction': inline_count / len(nonterminal) if nonterminal else None,
            'single_response_cases': sum(e['summary']['single_response'] for e in entries),
            'first_response_spills': sum(e['summary']['spill_requests'][:1] == [1] for e in entries),
            'returning_cases': sum(bool(e['summary']['return_requests']) for e in entries),
            'respilling_cases': sum(e['summary']['respills'] > 0 for e in entries),
            'faults': dict(faults), 'retry_fault_selector': {key: dict(value) for key, value in costs.items()}}
    return totals


def run(suite='core', workers=1):
    if type(workers) is not int or workers < 1:
        raise ValueError('workers must be a positive integer')
    sources = source_hashes()
    cases = fixtures(suite)
    tasks = schedule(suite, cases)
    if workers == 1:
        _initialize(suite)
        results = [_run_task(spec) for spec in tasks]
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_initialize, initargs=(suite,)) as pool:
            results = list(pool.map(_run_task, tasks))
    require(source_hashes() == sources, 'Reference source changed during the comparison; rerun from stable files.')
    return {'config': {'suite': suite, 'seed': SEED, 'batch_size': 50, 'candidate_budget': 1000,
                       'chain': 'recompute-comparison', 'retry_rate': 'one retry per accepted response'},
            'source_sha256': sources, 'totals': summarize_results(results), 'results': results}


def findings(report):
    """Deterministic evidence text generated solely from this report."""
    lines = ['# Placement comparison', '',
             'Fixed-data correctness and serialized storage measurements, using the actual core and grouped '
             'selectors. All three contracts use the same chain, plan, parameters, threshold and backup setting. '
             'Every healthy boundary is compared with an independent oracle and checked for safe retirement. '
             'These are synthetic fixtures, not latency benchmarks or production size estimates.', '',
             f"Suite `{report['config']['suite']}`: {report['totals']['cases']} cases, "
             f"{report['totals']['protocol_traversals']} complete healthy/fault traversals.", '',
             'One-way is the default contract. Two-way and replay-by-recompute are optional. The one-way v1 '
             'envelope differs from the optional v2 envelopes, so their switch points and token sizes may differ. '
             'All logical states, rows and healthy selector work must match. The two optional contracts also '
             'have identical token byte lengths and placement decisions.', '',
             '## Coverage', '',
             '| Strategy | Healthy requests | Single-response cases | First-response spills | Cases returning inline | Cases re-spilling | Inline / nonterminal responses |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for mode in MODES:
        values = report['totals']['strategies'][mode]
        lines.append('| ' + ' | '.join(map(str, (mode, *(values[key] for key in
                     ('healthy_requests', 'single_response_cases', 'first_response_spills', 'returning_cases',
                      'respilling_cases')), f"{values['inline_nonterminal_responses']} / {values['nonterminal_responses']}"))) + ' |')
    lines += ['', 'Inline fractions count nonterminal responses only; terminal responses have no continuation '
              'and do not count as inline. A terminal-only case has no inline fraction. Single-response '
              'cases are reported separately from transition coverage.', '', '## Core comparisons', '',
              'Each cell compares the same input chain. Positive savings mean less storage than one-way; '
              'negative savings mean more. The two-cycle fixture uses ordinary coverage growth and retirement; '
              'it does not change a horizon.', '',
              '| Fixture | Page | T bytes | Backup | One-way byte-pages | Two-way byte-pages | Recompute byte-pages | Recompute saving vs one-way |',
              '| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: |']
    for case in report['results']:
        if case['category'] == 'core':
            sizes = [case['strategies'][mode]['summary']['total_byte_pages'] for mode in MODES]
            saved = sizes[0] - sizes[2]
            percent = f'{100 * saved / sizes[0]:.2f}%' if sizes[0] else 'n/a'
            lines.append('| ' + ' | '.join(map(str, (case['name'], case['page_size'], case['threshold_bytes'],
                         case['backup_enabled'], *sizes, f'{saved} ({percent})'))) + ' |')
    grouped = [case for case in report['results'] if case['category'] == 'grouped']
    if grouped:
        buckets = defaultdict(list)
        for case in grouped:
            buckets[(case['page_size'], case['threshold_bytes'])].append(case)
        lines += ['', '## Grouped comparisons and overhead', '',
                  'These fixtures have 200 groups of 12 members and explicit pending reservation IDs. '
                  'The table separates first-response spills and later returns instead of assuming that every '
                  'grouped workload behaves alike. The small-page rows select alternating-member subtract '
                  'in all three coverage encodings; page 50 includes all eight distributions and policies.', '',
                  '| Page | T bytes | Cases | Recompute first spills | Recompute returns | Improve / worse / equal vs two-way | Saved vs one-way | Saved vs two-way | Added head metadata vs two-way | Payload removed vs two-way |',
                  '| ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |']
        for (page, threshold), cases in sorted(buckets.items()):
            saved = [case['comparisons']['two_way_to_recompute']['saved'] for case in cases]
            signs = [sum((value['total_byte_pages'] > 0 if sign == 1 else
                          value['total_byte_pages'] < 0 if sign == -1 else
                          value['total_byte_pages'] == 0) for value in saved) for sign in (1, -1, 0)]
            values = (page, threshold, len(cases),
                      sum(case['strategies']['recompute']['summary']['spill_requests'][:1] == [1] for case in cases),
                      sum(bool(case['strategies']['recompute']['summary']['return_requests']) for case in cases),
                      ' / '.join(map(str, signs)),
                      sum(case['comparisons']['one_way_to_recompute']['saved']['total_byte_pages'] for case in cases),
                      sum(s['total_byte_pages'] for s in saved), -sum(s['head_metadata_byte_pages'] for s in saved),
                      sum(s['response_payload_byte_pages'] for s in saved))
            lines.append('| ' + ' | '.join(map(str, values)) + ' |')
        lines += ['', 'A first-spill inline-input response can lose its full replay payload under recomputation. '
                  'Later handle inputs still keep full responses, with additional replay metadata. If a chain '
                  'never returns inline, the repeated metadata cost can exceed that one initial saving. '
                  '`pages.csv` records every component at every response, including terminal heads. '
                  'This explains negative small-page rows without treating them as a failed correctness check.']
    lines += ['', '## Retry costs', '',
              'The failure traversal retries every accepted request exactly once through a fresh service instance, '
              'including terminal responses: a deliberate 100% retry schedule, not a predicted retry rate. '
              'Inline requests with no accepted server head select again in every contract. After the first '
              'spill has been accepted, one-way and full-response two-way replay its stored response; '
              'recompute selects again from that original inline input and verifies the response digest. '
              'The same distinction applies to accepted managed inline inputs after a return. Handle-input '
              'retries replay the retained response.', '',
              '| Strategy | Retry selector calls | Retry consumed | Retry fetched | Retry source reads |',
              '| --- | ---: | ---: | ---: | ---: |']
    for mode in MODES:
        retry = report['totals']['strategies'][mode]['retry_fault_selector'].get('retry', {})
        lines.append('| ' + ' | '.join(map(str, (mode, *(retry.get(key, 0) for key in
                     ('select_calls', 'consumed', 'fetched', 'reads'))))) + ' |')
    lines += ['', 'At switches, after returns, selected handle inputs and handle-to-terminal, separate phases '
              'inject pre-publication failure, concurrent preparation, lost accepted responses and a competing '
              'commit. Hot-state eviction exercises backup recovery. Missing-state and stale-input probes '
              'reject before selection. The JSON partitions this work from the one-retry costs above.', '',
              '## Meaning and limits', '',
              'A byte-page is one byte retained at one response boundary, including terminal replay state. '
              'The report sums canonical records, not Python object sizes, RAM use or elapsed residency. `hot`, `backup`, '
              '`head_metadata`, `return_bridge_payload`, `other_response_payload` and `redirects` form a '
              'disjoint partition of total storage. The return bridge is the full handle-to-inline response '
              'retained until the next successful advance. Peaks, token bytes and dedicated-vector resident '
              'pages are included separately; releasing a vector does not by itself release replay metadata.', '',
              'Two-way return can remove dedicated vectors when state shrinks sufficiently after spilling. '
              'Recomputation can also remove an inline-input response payload on the first spill. Whether '
              'either saves total storage depends on that payload, later metadata, backup policy and traversal '
              'length. Retry calls, consumed candidates, fetched IDs and source reads describe these adapters, '
              'not SQL queries, latency or production CPU. Recompute requires deterministic selection; '
              'digest verification does not create a snapshot or durable storage.', '',
              f"Reproduce: `python -S -B -m reference.placement.comparison --suite {report['config']['suite']}`. "
              'Use `--workers N` to parallelize independent cases. Output must be a new directory. '
              'All generated outputs remain under `reference/results/` by default.', '']
    return '\n'.join(lines)


def write_report(report, output):
    output = Path(output)
    if output.exists():
        raise ValueError('Choose a new output directory; existing evidence is never overwritten.')
    output.mkdir(parents=True)
    (output / 'comparison.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    (output / 'FINDINGS.md').write_text(findings(report), encoding='utf-8')
    prefix = ['scenario', 'category', 'representation', 'page_size', 'threshold_bytes', 'backup_enabled', 'strategy']
    detail = list(report['results'][0]['strategies']['one_way']['pages'][0])
    retry_fields = ['retry_selector_calls', 'retry_consumed', 'retry_fetched', 'retry_reads']
    with (output / 'pages.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=prefix + detail + retry_fields)
        writer.writeheader()
        for case in report['results']:
            for mode in MODES:
                strategy = case['strategies'][mode]
                for page, retry in zip(strategy['pages'], strategy['failures']['retry_pages']):
                    writer.writerow({'scenario': case['name'], 'strategy': mode,
                                     **{key: case[key] for key in prefix if key not in ('scenario', 'strategy')},
                                     **page, **{'retry_' + key: retry[key]
                                                for key in ('selector_calls', 'consumed', 'fetched', 'reads')}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('smoke', 'core', 'grouped', 'full'), default='core')
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error('--workers must be positive')
    output = args.output if args.output is not None else REFERENCE / 'results/placement' / args.suite
    if output.exists():
        parser.error('Choose a new --output directory; existing evidence is never overwritten.')
    report = run(args.suite, args.workers)
    write_report(report, output)
    print(f"PASS {report['totals']['cases']} cases, {report['totals']['protocol_traversals']} traversals: {output}")


if __name__ == '__main__':
    main()
