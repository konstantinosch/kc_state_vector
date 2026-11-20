"""Public packaging and runner checks; not semantic mutation detectors."""
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from reference.grouped import run_checks, run_hybrid_checks
from reference.grouped.scenarios import targeted_scenarios


ROOT = Path(__file__).resolve().parents[1]
MODES = ('ids', 'spans', 'hybrid')


class PublicExportTests(unittest.TestCase):
    def _python(self, arguments, *, flags=()):
        environment = dict(os.environ)
        for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONOPTIMIZE'):
            environment.pop(key, None)
        return subprocess.run(
            [sys.executable, '-X', 'utf8', '-S', '-B', *flags, *arguments],
            cwd=ROOT.parent.parent, env=environment, capture_output=True,
            text=True, encoding='utf-8', timeout=300,
        )

    def _one_case(self, runner):
        stack = ExitStack()
        case = next(case for case in targeted_scenarios()
                    if case.name == 'middle_hole_subtract')
        stack.enter_context(patch.object(runner, 'targeted_scenarios', return_value=(case,)))
        stack.enter_context(patch.object(runner, 'generated_scenarios', return_value=()))
        stack.enter_context(patch.object(runner, 'distribution_scenarios', return_value=()))
        if runner is run_hybrid_checks:
            stack.enter_context(patch.object(runner, 'hybrid_scenarios', return_value=()))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return stack

    def test_exported_sources_match_the_recorded_hashes(self):
        manifest = json.loads((ROOT / 'provenance.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['schema_version'], 1)
        self.assertEqual(set(manifest['files']),
                         {path.relative_to(ROOT).as_posix() for path in ROOT.rglob('*.py')})
        for relative, entry in manifest['files'].items():
            with self.subTest(path=relative):
                path = Path(relative)
                self.assertFalse(path.is_absolute())
                self.assertNotIn('..', path.parts)
                self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(),
                                 entry['sha256'])
        for name in ('audit.py', 'coverage.py', 'oracle.py'):
            self.assertEqual(manifest['files'][name]['sha256'],
                             manifest['files'][name]['origin_sha256'])

    def test_worked_example_runs_both_policies_in_every_representation(self):
        expected_rows = {
            'subtract': [['early', 'A', ['A', 'C']], ['owner', 'B', ['B']], ['rest', 'X', ['X']]],
            'defer': [['owner', 'B', ['A', 'B', 'C']], ['rest', 'X', ['X']]],
        }
        for policy, expected in expected_rows.items():
            for representation in MODES:
                with self.subTest(policy=policy, representation=representation):
                    result = self._python(['-m', 'reference.grouped', '--policy', policy,
                                           '--representation', representation, '--page-size', '1',
                                           '--candidate-budget', '1', '--show-state'])
                    self.assertEqual(result.returncode, 0, result.stderr)
                    output = [json.loads(line) for line in result.stdout.splitlines()]
                    self.assertEqual([row for page in output[:-1] for row in page['rows']], expected)
                    self.assertIsNone(output[-2]['state'])
                    self.assertEqual(output[-1], {'oracle_match': True, 'policy': policy,
                                                 'representation': representation,
                                                 'rows': len(expected), 'represented_members': 4})

    def test_optimized_grouped_entrypoints_inherit_the_core_guard(self):
        for module in ('reference.grouped', 'reference.grouped.run_checks',
                       'reference.grouped.run_hybrid_checks', 'reference.grouped.mutations'):
            with self.subTest(module=module):
                result = self._python(['-m', module, '--help'], flags=('-O',))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('reference requires assertions enabled.', result.stderr)
                self.assertEqual(result.stdout, '')

    def test_default_reports_live_under_the_ignored_result_directory(self):
        self.assertEqual(run_checks.DEFAULT_OUTPUT, ROOT.parent / 'results/grouped/ids-spans')
        self.assertEqual(run_hybrid_checks.DEFAULT_OUTPUT, ROOT.parent / 'results/grouped/hybrid')

    def test_hybrid_runner_does_not_read_preceding_evidence_implicitly(self):
        with self._one_case(run_hybrid_checks), patch.object(
                Path, 'read_text', side_effect=AssertionError('unexpected file read')):
            results, matched = run_hybrid_checks.run(generated_cases=0)
        self.assertEqual(len(results), 3)
        self.assertEqual(matched, 0)
        self.assertTrue(all(result['oracle_match'] for result in results))

    def test_explicit_previous_evidence_is_checked_hashed_and_preserved(self):
        with self._one_case(run_checks):
            previous = run_checks.run(generated_cases=0)
        with tempfile.TemporaryDirectory() as folder:
            previous_path = Path(folder) / 'previous.json'
            previous_bytes = json.dumps({'results': previous}, sort_keys=True).encode('utf-8')
            previous_path.write_bytes(previous_bytes)
            with self._one_case(run_hybrid_checks):
                results, matched = run_hybrid_checks.run(
                    generated_cases=0, previous_comparison=previous_path)
            self.assertEqual(matched, len(previous))
            output = Path(folder) / 'new'
            run_hybrid_checks.write_results(results, output, {}, matched,
                                            previous_comparison=previous_path)
            report = json.loads((output / 'comparison.json').read_text(encoding='utf-8'))
            self.assertEqual(report['previous_results_sha256'],
                             {'previous.json': hashlib.sha256(previous_bytes).hexdigest()})
            self.assertEqual(previous_path.read_bytes(), previous_bytes)
            previous[0]['rows'] += 1
            previous_path.write_text(json.dumps({'results': previous}), encoding='utf-8')
            with self._one_case(run_hybrid_checks), self.assertRaises(AssertionError):
                run_hybrid_checks.run(generated_cases=0, previous_comparison=previous_path)
            previous[0]['rows'] -= 1
            previous[0]['representations'] = ['ids', 'unsupported-mode']
            previous_path.write_text(json.dumps({'results': previous}), encoding='utf-8')
            with self._one_case(run_hybrid_checks), self.assertRaises(AssertionError):
                run_hybrid_checks.run(generated_cases=0, previous_comparison=previous_path)

    def test_previous_evidence_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder)
            previous_path = destination / 'comparison.json'
            previous_path.write_bytes(b'preserve previous evidence')
            with self.assertRaisesRegex(ValueError, 'preserves the preceding comparison'):
                run_hybrid_checks.write_results([], destination, {}, 0,
                                                previous_comparison=previous_path)
            self.assertEqual(previous_path.read_bytes(), b'preserve previous evidence')

    def test_missing_explicit_previous_evidence_is_an_error(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(FileNotFoundError):
            run_hybrid_checks.run(previous_comparison=Path(folder) / 'missing.json')


if __name__ == '__main__':
    unittest.main()
