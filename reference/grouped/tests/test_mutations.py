"""Focused grouped fault witnesses and mutation-tooling failure guards."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reference import Reservation
from reference.grouped.coverage import expand_coverage
from reference.grouped.model import Pager, Plan, Record, Stage
from reference.grouped.mutations import Mutation, main, run_checks
from reference.grouped.scenarios import targeted_scenarios


class GroupedMutationRegressions(unittest.TestCase):
    def covered_owner_plan(self):
        return Plan(
            (Record('A', 3, 'G'), Record('B', 2, 'G'), Record('X', 1, 'H')),
            (Stage('early', ('A', 'X')), Stage('owner', ('B',), quota=0),
             Stage('rest', ('X',), mode='items')),
            (Reservation('owner', ('B',), yield_from=('rest',), on_unserved='omit'),),
        )

    def test_pending_zero_quota_owner_retains_covered_member(self):
        plan = self.covered_owner_plan()
        for mode in ('ids', 'spans', 'hybrid'):
            with self.subTest(mode=mode):
                first = Pager(plan, mode).page(page_size=1, candidate_budget=1)
                self.assertEqual(first.rows, (('early', 'A', ('A', 'B')),))
                self.assertIn('B', first.state['pending'])
                self.assertIn('B', expand_coverage(plan.records, first.state['coverage'], mode))

    def test_owner_completion_does_not_omit_covered_member(self):
        plan = self.covered_owner_plan()
        for mode in ('ids', 'spans', 'hybrid'):
            with self.subTest(mode=mode):
                page = Pager(plan, mode).page(page_size=2, candidate_budget=2)
                self.assertEqual(page.rows, (('early', 'A', ('A', 'B')), ('early', 'X', ('X',))))
                self.assertIsNotNone(page.state)
                self.assertEqual(page.state['stage'], 2)
                self.assertNotIn('B', page.state['omitted'])
                self.assertEqual(page.state['pending'], [])

    def test_unaffected_stage_can_represent_reserved_member(self):
        plan = next(case.plan for case in targeted_scenarios()
                    if case.name == 'selective_yield_preemption')
        for mode in ('ids', 'spans', 'hybrid'):
            with self.subTest(mode=mode):
                first = Pager(plan, mode).page(page_size=1, candidate_budget=1)
                self.assertEqual(first.rows, (('early', 'A', ('A', 'B', 'C')),))


class GroupedMutationToolTests(unittest.TestCase):
    def package(self, temporary):
        root = Path(temporary)
        (root / 'grouped').mkdir()
        (root / 'grouped/model.py').write_bytes(b'value = 1\n')
        return root

    def test_survivor_fails_without_editing_original(self):
        mutation = Mutation('change', 'test', 'value = 1', 'value = 2', ('test',))
        with tempfile.TemporaryDirectory() as temporary:
            package = self.package(temporary)
            with patch('reference.grouped.mutations._run_tests', side_effect=[
                {'status': 'passed'}, {'status': 'survived'},
            ]):
                report = run_checks(package=package, mutations=(mutation,))
            self.assertEqual(report['status'], 'failed')
            self.assertTrue(report['source_unchanged'])
            self.assertEqual((package / 'grouped/model.py').read_bytes(), b'value = 1\n')

    def test_stale_or_invalid_target_cannot_be_silently_skipped(self):
        for mutation in (Mutation('stale', 'test', 'missing', 'different', ('test',)),
                         Mutation('escape', 'test', '1', '2', ('test',), '../model.py')):
            with self.subTest(mutation=mutation.name), tempfile.TemporaryDirectory() as temporary:
                package = self.package(temporary)
                with patch('reference.grouped.mutations._run_tests') as worker:
                    report = run_checks(package=package, mutations=(mutation,))
                self.assertEqual(report['status'], 'failed')
                worker.assert_not_called()

    def test_failed_control_stops_variants_and_errors_do_not_count_as_detection(self):
        mutation = Mutation('change', 'test', 'value = 1', 'value = 2', ('test',))
        for outcomes, calls in (([{'status': 'baseline_failed'}], 1),
                                ([{'status': 'passed'}, {'status': 'infrastructure_error'}], 2)):
            with self.subTest(outcomes=outcomes), tempfile.TemporaryDirectory() as temporary:
                package = self.package(temporary)
                with patch('reference.grouped.mutations._run_tests', side_effect=outcomes) as worker:
                    report = run_checks(package=package, mutations=(mutation,))
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(worker.call_count, calls)

    def test_cli_failure_and_report_guard(self):
        report = {'status': 'failed', 'baseline': {'status': 'passed'},
                  'mutations': [{'name': 'example', 'status': 'survived'}]}
        with patch('reference.grouped.mutations.run_checks', return_value=report), redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 1)
        target = Path(__file__).resolve().parents[1] / 'model.py'
        before = target.read_bytes()
        with patch('reference.grouped.mutations.run_checks') as runner, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(['--json', str(target)])
        self.assertEqual(raised.exception.code, 2)
        runner.assert_not_called()
        self.assertEqual(target.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
