"""Behavioral checks for the portable checker, using deliberately small adapters.

The candidate adapters below do not import the oracle or the sample pager. Tests
exercise accepted behavior and failure reports rather than checker source text.
"""

from copy import deepcopy
from contextlib import redirect_stdout
import io
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


KIT = Path(__file__).resolve().parents[2] / 'skills/kc-state-vector/scripts'
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))
import check_pager as checker
_mutation_spec = importlib.util.spec_from_file_location('kc_skill_mutations_tests', KIT / 'mutations.py')
mutations = importlib.util.module_from_spec(_mutation_spec)
_mutation_spec.loader.exec_module(mutations)


def fixture(ids=('A', 'B'), *, members=None):
    return {
        'schema_version': 1,
        'name': 'checker_witness',
        'records': [{'id': identity, 'sort_key': len(ids) - index}
                    for index, identity in enumerate(ids)],
        'stages': [{'name': 'all', 'ids': list(ids if members is None else members), 'quota': None}],
    }


def schedule(*, pages=(1,), batches=(3,), budgets=(1,)):
    return {'name': 'witness', 'page_sizes': list(pages),
            'batch_sizes': list(batches), 'candidate_budgets': list(budgets)}


def adapter(factory, capabilities=()):
    return SimpleNamespace(CAPABILITIES=set(capabilities), make_pager=factory)


def sliced_factory(rows=('A', 'B')):
    """A plain cursor/page-size adapter with no optional keyword parameters."""
    def factory(_fixture):
        def page(continuation, *, page_size):
            offset = 0 if continuation is None else continuation['offset']
            selected = deepcopy(list(rows[offset:offset + page_size]))
            next_offset = offset + len(selected)
            return {'rows': selected,
                    'continuation': None if next_offset == len(rows) else {'offset': next_offset}}
        return page
    return factory


WORKER_ADAPTER = '''
def make_pager(fixture):
    def page(continuation, *, page_size):
        offset = 0 if continuation is None else continuation['offset']
        selected = ['A', 'B'][offset:offset + page_size]
        next_offset = offset + len(selected)
        return {'rows': selected,
                'continuation': None if next_offset == 2 else {'offset': next_offset}}
    return page
'''


class SelfCheckTests(unittest.TestCase):
    def check(self, candidate, *, data=None, request=None, max_requests=None):
        return checker.evaluate(candidate, fixture() if data is None else data,
                                schedule() if request is None else request,
                                max_requests=max_requests)

    def assert_pass(self, report):
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(report['exit_code'], 0, report)

    def assert_fault(self, report, kind, *, infrastructure=False):
        self.assertEqual(report['status'], 'error' if infrastructure else 'failed', report)
        self.assertEqual(report['exit_code'], 2 if infrastructure else 1, report)
        self.assertEqual(report['failure']['kind'], kind, report)

    def test_required_tier_calls_only_page_size_and_reports_skipped_capabilities(self):
        report = self.check(adapter(sliced_factory()), request=schedule(pages=(1, 3), batches=(1, 3)))
        self.assert_pass(report)
        self.assertEqual(report['capabilities'], [])
        self.assertIn('id_sequence', report['checks_run'])
        self.assertIn('retry', report['checks_run'])
        self.assertIn('continuation_roundtrip', report['checks_run'])
        self.assertIn('stage_attribution', report['checks_skipped'])
        self.assertIn('candidate_budget', report['checks_skipped'])
        self.assertIn('batch_variation', report['checks_skipped'])

    def test_missing_capabilities_attribute_means_required_tier(self):
        self.assert_pass(self.check(SimpleNamespace(make_pager=sliced_factory())))

    def test_opaque_string_continuation_is_supported(self):
        def factory(_fixture):
            def page(continuation, *, page_size):
                offset = 0 if continuation is None else int(continuation)
                chosen = ['A', 'B'][offset:offset + page_size]
                after = offset + len(chosen)
                return {'rows': chosen, 'continuation': None if after == 2 else str(after)}
            return page
        self.assert_pass(self.check(adapter(factory)))

    def test_unclaimed_stage_labels_do_not_become_an_attribution_claim(self):
        rows = ({'stage': 'not-the-owner', 'id': 'A'}, {'stage': 'not-the-owner', 'id': 'B'})
        report = self.check(adapter(sliced_factory(rows)))
        self.assert_pass(report)
        self.assertIn('stage_attribution', report['checks_skipped'])
        self.assertNotIn('stage_attribution', report['checks_run'])

    def test_declared_labels_check_actual_stage_attribution(self):
        wrong = ({'stage': 'other', 'id': 'A'}, {'stage': 'all', 'id': 'B'})
        report = self.check(adapter(sliced_factory(wrong), ('stage_labels',)))
        self.assert_fault(report, 'row_mismatch')
        right = ({'stage': 'all', 'id': 'A'}, {'stage': 'all', 'id': 'B'})
        self.assert_pass(self.check(adapter(sliced_factory(right), ('stage_labels',))))

    def test_stage_capability_requires_labeled_rows(self):
        report = self.check(adapter(sliced_factory(), ('stage_labels',)))
        self.assert_fault(report, 'invalid_response')

    def test_first_wrong_id_fails_at_its_first_position(self):
        report = self.check(adapter(sliced_factory(('B', 'A'))))
        self.assert_fault(report, 'row_mismatch')
        self.assertEqual(report['failure']['request'], 1)

    def test_extra_row_is_not_accepted(self):
        report = self.check(adapter(sliced_factory(('A', 'B', 'C'))),
                            data=fixture(('A', 'B', 'C'), members=('A', 'B')),
                            request=schedule(pages=(50,)))
        self.assert_fault(report, 'extra_row')

    def test_early_terminal_is_not_a_short_success(self):
        self.assert_fault(self.check(adapter(sliced_factory(('A',)))), 'early_exhaustion')

    def test_empty_page_with_continuation_is_followed(self):
        calls = []
        def factory(_fixture):
            def page(continuation, *, page_size):
                calls.append(deepcopy(continuation))
                if continuation is None:
                    return {'rows': [], 'continuation': {}}
                offset = continuation.get('offset', 0)
                chosen = ['A', 'B'][offset:offset + page_size]
                after = offset + len(chosen)
                return {'rows': chosen,
                        'continuation': None if after == 2 else {'offset': after}}
            return page
        self.assert_pass(self.check(adapter(factory)))
        self.assertTrue(any(value == {} for value in calls))
        # Initial null was retried, not called again after terminal output.
        self.assertEqual(sum(value is None for value in calls), 2)

    def test_repeated_empty_nonterminal_state_is_nonprogress(self):
        def factory(_fixture):
            return lambda continuation, *, page_size: {'rows': [], 'continuation': {'offset': 0}}
        self.assert_fault(self.check(adapter(factory)), 'nonprogress')

    def test_terminal_empty_plan_is_valid(self):
        data = fixture(())
        data['stages'] = []
        self.assert_pass(self.check(adapter(sliced_factory(())), data=data))

    def test_fixture_mutation_during_factory_is_detected(self):
        def factory(data):
            data['records'].clear()
            return sliced_factory()(data)
        self.assert_fault(self.check(adapter(factory)), 'input_mutation')

    def test_fixture_mutation_during_page_is_detected(self):
        def factory(data):
            def page(continuation, *, page_size):
                data['stages'][0]['ids'].clear()
                return {'rows': ['A', 'B'], 'continuation': None}
            return page
        self.assert_fault(self.check(adapter(factory), request=schedule(pages=(50,))), 'input_mutation')

    def test_input_continuation_mutation_is_detected(self):
        def factory(_fixture):
            def page(continuation, *, page_size):
                if continuation is None:
                    return {'rows': ['A'], 'continuation': {'offset': 1}}
                continuation['offset'] = 2
                return {'rows': ['B'], 'continuation': None}
            return page
        self.assert_fault(self.check(adapter(factory)), 'input_mutation')

    def test_fresh_factory_is_used_for_each_request_and_retry(self):
        constructions = []
        def factory(data):
            constructions.append(object())
            return sliced_factory()(data)
        self.assert_pass(self.check(adapter(factory)))
        self.assertEqual(len(constructions), 4)

    def test_retry_output_is_compared_without_being_appended(self):
        counter = [0]
        def factory(_fixture):
            def page(continuation, *, page_size):
                counter[0] += 1
                return {'rows': ['A'], 'continuation': {'nonce': counter[0]}}
            return page
        self.assert_fault(self.check(adapter(factory)), 'retry_mismatch')

    def test_retry_comparison_preserves_json_value_types(self):
        counter = [0]
        def factory(_fixture):
            def page(continuation, *, page_size):
                counter[0] += 1
                return {'rows': ['A'], 'continuation': {'offset': 1 if counter[0] == 1 else True}}
            return page
        # Python's loose equality says 1 == True. JSON continuation identity must not.
        self.assert_fault(self.check(adapter(factory)), 'retry_mismatch')

    def test_budget_capability_passes_the_limit_and_checks_accounting(self):
        seen = []
        def factory(_fixture):
            def page(continuation, *, page_size, candidate_budget):
                seen.append(candidate_budget)
                offset = 0 if continuation is None else continuation['offset']
                chosen = ['A', 'B'][offset:offset + min(page_size, candidate_budget)]
                after = offset + len(chosen)
                return {'rows': chosen, 'consumed': len(chosen),
                        'continuation': None if after == 2 else {'offset': after}}
            return page
        report = self.check(adapter(factory, ('candidate_budget',)), request=schedule(pages=(3,)))
        self.assert_pass(report)
        self.assertEqual(set(seen), {1})
        self.assertIn('candidate_budget', report['checks_run'])

    def test_budget_capability_requires_consumed(self):
        def factory(_fixture):
            return lambda continuation, *, page_size, candidate_budget: {'rows': ['A'], 'continuation': {'offset': 1}}
        self.assert_fault(self.check(adapter(factory, ('candidate_budget',))), 'invalid_response')

    def test_reported_consumption_cannot_exceed_budget(self):
        def factory(_fixture):
            return lambda continuation, *, page_size, candidate_budget: {
                'rows': ['A'], 'continuation': {'offset': 1}, 'consumed': candidate_budget + 1}
        self.assert_fault(self.check(adapter(factory, ('candidate_budget',))), 'bounds')

    def test_short_nonterminal_budget_page_must_spend_budget(self):
        def factory(_fixture):
            return lambda continuation, *, page_size, candidate_budget: {
                'rows': [], 'continuation': {'offset': 0}, 'consumed': 0}
        self.assert_fault(self.check(adapter(factory, ('candidate_budget',))), 'bounds')

    def test_batch_capability_passes_requested_batch_size(self):
        batches = []
        def factory(data):
            base = sliced_factory()(data)
            def page(continuation, *, page_size, batch_size):
                batches.append(batch_size)
                return base(continuation, page_size=page_size)
            return page
        report = self.check(adapter(factory, ('batch_size',)), request=schedule(batches=(3, 1)))
        self.assert_pass(report)
        self.assertEqual(batches, [3, 3, 1, 1])
        self.assertIn('batch_variation', report['checks_run'])

    def test_unknown_capability_is_configuration_error(self):
        self.assert_fault(self.check(adapter(sliced_factory(), ('invented',))),
                          'configuration', infrastructure=True)

    def test_adapter_exception_is_not_semantic_detection(self):
        def factory(_fixture):
            def page(continuation, *, page_size):
                raise RuntimeError('candidate failure')
            return page
        self.assert_fault(self.check(adapter(factory)), 'adapter_exception', infrastructure=True)

    def test_invalid_response_and_oversized_page_are_rejected(self):
        for value, kind in ((None, 'invalid_response'),
                            ({'rows': ['A', 'B'], 'continuation': None}, 'bounds')):
            with self.subTest(value=value):
                def factory(_fixture):
                    return lambda continuation, *, page_size: deepcopy(value)
                self.assert_fault(self.check(adapter(factory)), kind)

    def test_continuation_must_be_an_opaque_json_object_or_nonempty_string(self):
        for continuation in (False, [], '', {'unsupported': {1, 2}}):
            with self.subTest(continuation=continuation):
                def factory(_fixture):
                    return lambda incoming, *, page_size: {'rows': ['A'], 'continuation': continuation}
                self.assert_fault(self.check(adapter(factory)), 'invalid_response')

    def test_continuation_size_guard_is_incomplete_not_semantic_detection(self):
        def factory(_fixture):
            return lambda continuation, *, page_size: {
                'rows': ['A'], 'continuation': {'padding': 'x' * (1024 * 1024)}}
        self.assert_fault(self.check(adapter(factory)), 'size_limit', infrastructure=True)

    def test_json_container_depth_accepts_64_levels_with_scalar_or_empty_leaf(self):
        for leaf in ('value', None, 7, False, {}, []):
            with self.subTest(leaf=leaf):
                value = deepcopy(leaf)
                wrappers = 63 if type(leaf) in (dict, list) else 64
                for index in range(wrappers):
                    value = {'child': value} if index % 2 else [value]
                encoded = checker.canonical(value)
                self.assertIsInstance(encoded, str)

    def test_json_container_depth_rejects_65_levels_even_with_empty_leaf(self):
        for leaf in ('value', {}, []):
            with self.subTest(leaf=leaf):
                value = deepcopy(leaf)
                wrappers = 64 if type(leaf) in (dict, list) else 65
                for index in range(wrappers):
                    value = {'child': value} if index % 2 else [value]
                with self.assertRaises(checker.CheckError) as caught:
                    checker.canonical(value)
                self.assertEqual(caught.exception.details['kind'], 'size_limit')

    def test_64_level_continuation_is_accepted_inside_response_envelope(self):
        def factory(_fixture):
            def page(continuation, *, page_size):
                if continuation is not None:
                    return {'rows': ['B'], 'continuation': None}
                nested = None
                for _ in range(63):
                    nested = {'child': nested}
                return {'rows': ['A'], 'continuation': {'offset': 1, 'nested': nested}}
            return page
        self.assert_pass(self.check(adapter(factory)))

    def test_boolean_consumed_is_not_an_integer_counter(self):
        def factory(_fixture):
            return lambda continuation, *, page_size, candidate_budget: {
                'rows': ['A'], 'continuation': {'offset': 1}, 'consumed': True}
        self.assert_fault(self.check(adapter(factory, ('candidate_budget',))), 'invalid_response')

    def test_invalid_schedule_is_rejected_before_calling_adapter(self):
        called = []
        def factory(data):
            called.append(True)
            return sliced_factory()(data)
        for pages in ([], [0], [True], [1.0]):
            with self.subTest(pages=pages):
                self.assert_fault(self.check(adapter(factory), request=schedule(pages=pages)),
                                  'configuration', infrastructure=True)
        self.assertEqual(called, [])

    def test_guard_expiry_is_incomplete_not_a_pagination_kill(self):
        def factory(_fixture):
            def page(continuation, *, page_size):
                n = 0 if continuation is None else continuation['n']
                return {'rows': [], 'continuation': {'n': n + 1}}
            return page
        self.assert_fault(self.check(adapter(factory), max_requests=3), 'request_limit', infrastructure=True)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kc-kit-checker-test-')
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'adapter.py'

    def run_worker(self, source, *, timeout=300):
        self.path.write_text(source, encoding='utf-8')
        return checker.run_case(self.path, fixture(), schedule(), timeout=timeout)

    def test_clean_worker_and_fresh_process_resume(self):
        report = self.run_worker(WORKER_ADAPTER)
        self.assertEqual(report['exit_code'], 0, report)
        self.assertEqual(report['status'], 'passed', report)
        self.assertIn('fresh_process_resume', report['checks_run'])

    def test_large_valid_continuation_uses_protocol_not_diagnostic_limit(self):
        source = WORKER_ADAPTER.replace("{'offset': next_offset}",
                                        "{'offset': next_offset, 'padding': 'x' * 80000}")
        report = self.run_worker(source)
        self.assertEqual(report['status'], 'passed', report)

    def test_64_level_continuation_survives_worker_envelope_and_fresh_resume(self):
        prefix = '''
def continuation_value(offset):
    nested = None
    for _ in range(63):
        nested = {'child': nested}
    return {'offset': offset, 'nested': nested}
'''
        source = prefix + WORKER_ADAPTER.replace("{'offset': next_offset}",
                                                 'continuation_value(next_offset)')
        report = self.run_worker(source)
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(report['exit_code'], 0, report)
        self.assertIn('fresh_process_resume', report['checks_run'])

    def test_import_error_is_infrastructure_failure(self):
        report = self.run_worker("raise RuntimeError('import failed')\n")
        self.assertEqual(report['status'], 'error', report)
        self.assertEqual(report['exit_code'], 2, report)

    def test_optimized_python_is_rejected_before_candidate_loading(self):
        self.path.write_text("raise RuntimeError('candidate was imported')\n", encoding='utf-8')
        result = subprocess.run([sys.executable, '-S', '-B', '-O', str(KIT / 'check_pager.py'),
                                 '--adapter', str(self.path)], capture_output=True, text=True,
                                encoding='utf-8', timeout=300)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('assertions enabled', result.stderr)
        self.assertNotIn('candidate was imported', result.stderr)

    def test_hung_worker_is_stopped_without_semantic_detection(self):
        source = 'def make_pager(fixture):\n    while True:\n        pass\n'
        report = self.run_worker(source, timeout=0.25)
        self.assertEqual(report['status'], 'error', report)
        self.assertEqual(report['exit_code'], 2, report)
        self.assertIn('timeout', repr(report).lower())

    def test_diagnostic_overflow_is_not_a_success_or_semantic_detection(self):
        report = self.run_worker("print('x' * 70000)\n" + WORKER_ADAPTER)
        self.assertEqual(report['status'], 'error', report)
        self.assertEqual(report['exit_code'], 2, report)

    def test_live_diagnostic_reader_after_worker_exit_is_incomplete(self):
        process = SimpleNamespace(stdout=Mock(), poll=Mock(return_value=0),
                                  wait=Mock(return_value=0), returncode=0)
        reader = Mock()
        reader.is_alive.return_value = True
        def start_worker(command, **kwargs):
            Path(command[-1]).write_text('{"status":"passed","exit_code":0}', encoding='utf-8')
            return process
        with patch.object(checker.subprocess, 'Popen', side_effect=start_worker), \
                patch.object(checker.threading, 'Thread', return_value=reader):
            report = checker._launch({'operation': 'metadata', 'adapter': str(self.path)}, 300)
        self.assertEqual(report['status'], 'error', report)
        self.assertEqual(report['exit_code'], 2, report)
        self.assertFalse(mutations.is_detected(report))
        reader.start.assert_called_once()
        reader.join.assert_called_once()

    def test_resume_does_not_depend_on_previous_worker_globals(self):
        source = '''
import copy
import json
offset = 0
responses = {}
def make_pager(fixture):
    def page(continuation, *, page_size):
        global offset
        key = json.dumps(continuation, sort_keys=True)
        if key in responses:
            return copy.deepcopy(responses[key])
        chosen = ['A', 'B'][offset:offset + page_size]
        offset += len(chosen)
        result = {'rows': chosen, 'continuation': None if offset == 2 else {'n': offset}}
        responses[key] = copy.deepcopy(result)
        return result
    return page
'''
        report = self.run_worker(source)
        self.assertEqual(report['status'], 'failed', report)
        self.assertEqual(report['exit_code'], 1, report)
        self.assertIn('resume', repr(report).lower())

    def test_evaluation_and_resume_share_one_traversal_deadline(self):
        response = {'rows': ['B'], 'continuation': None}
        initial = {
            'status': 'passed', 'exit_code': 0, 'checks_run': [], 'checks_skipped': {},
            'resume_witness': {'input': {'offset': 1}, 'parameters': {'page_size': 1},
                               'response': response},
        }
        for outer, expected in ((None, 107.0), (200.0, 107.0), (102.0, 102.0)):
            with self.subTest(outer=outer):
                with patch.object(checker.time, 'monotonic', return_value=100.0), \
                        patch.object(checker, '_launch', side_effect=[
                            deepcopy(initial), {'status': 'passed', 'exit_code': 0, 'response': response}
                        ]) as launcher:
                    report = checker.run_case(self.path, fixture(), schedule(), timeout=7.0, deadline=outer)
                self.assertEqual(report['exit_code'], 0, report)
                self.assertEqual([call.args[2] for call in launcher.call_args_list], [expected, expected])


class ConsoleReportTests(unittest.TestCase):
    def invoke_report(self, report):
        before = deepcopy(report)
        output = io.StringIO()
        with patch.object(checker, 'run', return_value=report), redirect_stdout(output):
            code = checker.main(['--adapter', 'unused.py'])
        self.assertEqual(report, before, 'Text rendering must not change the JSON report')
        return code, output.getvalue()

    def test_cli_wrong_row_names_fixture_schedule_and_first_divergence(self):
        with tempfile.TemporaryDirectory(prefix='kc-kit-cli-report-') as folder:
            root = Path(folder)
            candidate = root / 'adapter.py'
            candidate.write_text('''
def make_pager(fixture):
    def page(continuation, *, page_size):
        return {'rows': ['wrong'], 'continuation': None}
    return page
''', encoding='utf-8')
            report_path = root / 'report.json'
            result = subprocess.run([sys.executable, '-S', '-B', str(KIT / 'check_pager.py'),
                                     '--adapter', str(candidate), '--max-page-size', '1',
                                     '--json', str(report_path)], cwd=root,
                                    capture_output=True, text=True, encoding='utf-8', timeout=300)
            report = json.loads(report_path.read_text(encoding='utf-8'))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['failure']['expected'], '51')
        self.assertEqual(report['failure']['actual'], 'wrong')
        self.assertIn('FAILED at traversal 1 of 70', result.stdout)
        self.assertIn('stopped at first failure', result.stdout)
        for value in ('priority_51_49_48', 'schedule p1-b50-c50', 'request 1',
                      'page position 1', 'global position 1', 'expected "51", got "wrong"'):
            self.assertIn(value, result.stdout)
        self.assertNotIn('1/70 traversals', result.stdout)
        self.assertIn('\n  Data: fixed finite staged sources', result.stdout)
        self.assertIn('\n  Order: descending', result.stdout)
        self.assertNotIn("'data':", result.stdout)

    def test_success_describes_completed_traversals(self):
        report = {'status': 'passed', 'exit_code': 0, 'scheduled_traversals': 2,
                  'results': [{'status': 'passed'}, {'status': 'passed'}]}
        code, output = self.invoke_report(report)
        self.assertEqual(code, 0)
        self.assertIn('PASSED (scoped)', output)
        self.assertIn('all 2 scheduled traversals completed', output)
        self.assertNotIn('stopped', output)

    def test_failure_after_successes_identifies_the_failing_case(self):
        report = {'status': 'failed', 'exit_code': 1, 'scheduled_traversals': 100,
                  'results': [{'status': 'passed', 'fixture': 'earlier'},
                              {'status': 'failed', 'fixture': 'later',
                               'schedule': {'name': 'changing'}}],
                  'failure': {'kind': 'row_mismatch', 'message': 'First selection mismatch',
                              'request': 4, 'page_position': 2, 'global_position': 7,
                              'expected': {'stage': 'all', 'id': 'B'},
                              'actual': {'stage': 'priority', 'id': 'B'}}}
        code, output = self.invoke_report(report)
        self.assertEqual(code, 1)
        self.assertIn('FAILED at traversal 2 of 100', output)
        self.assertIn('later, schedule changing, request 4, page position 2, global position 7', output)
        self.assertIn('"stage": "all"', output)
        self.assertIn('"stage": "priority"', output)
        self.assertNotIn('earlier', output)

    def test_error_before_traversals_does_not_claim_a_failed_case(self):
        report = {'status': 'error', 'exit_code': 2, 'results': [],
                  'failure': {'kind': 'configuration', 'message': 'No adapter file'}}
        code, output = self.invoke_report(report)
        self.assertEqual(code, 2)
        self.assertIn('ERROR before traversal execution', output)
        self.assertIn('validation incomplete', output)
        self.assertIn('configuration: No adapter file', output)
        self.assertIn('Capabilities: not established', output)
        self.assertNotIn('FAILED', output)
        self.assertNotIn('schedule', output)

    def test_error_during_traversal_names_context_without_semantic_failure_claim(self):
        report = {'status': 'error', 'exit_code': 2, 'scheduled_traversals': 100,
                  'results': [{'status': 'error', 'fixture': 'limited', 'schedule': {'name': 'p1'}}],
                  'failure': {'kind': 'timeout', 'message': 'Worker deadline reached'}}
        code, output = self.invoke_report(report)
        self.assertEqual(code, 2)
        self.assertIn('ERROR at traversal 1 of 100', output)
        self.assertIn('validation incomplete', output)
        self.assertIn('limited, schedule p1', output)
        self.assertNotIn('stopped at first failure', output)

    def test_error_after_traversals_does_not_blame_last_passed_fixture(self):
        report = {'status': 'error', 'exit_code': 2, 'scheduled_traversals': 1,
                  'results': [{'status': 'passed', 'fixture': 'passed_fixture',
                               'schedule': {'name': 'passed_schedule'}}],
                  'failure': {'kind': 'configuration', 'message': 'Adapter source changed during the check'}}
        code, output = self.invoke_report(report)
        self.assertEqual(code, 2)
        self.assertIn('ERROR after 1 completed traversal', output)
        self.assertIn('validation incomplete', output)
        self.assertNotIn('passed_fixture', output)
        self.assertNotIn('passed_schedule', output)
        self.assertNotIn('PASSED', output)


class MutationToolTests(unittest.TestCase):
    def test_only_semantic_row_failures_count_as_detections(self):
        for kind in ('row_mismatch', 'early_exhaustion', 'extra_row'):
            with self.subTest(kind=kind):
                self.assertTrue(mutations.is_detected({'status': 'failed', 'exit_code': 1,
                                                       'failure': {'kind': kind}}))
        for kind in ('timeout', 'size_limit', 'request_limit', 'output_limit', 'worker_error',
                     'configuration', 'adapter_exception', 'invalid_response', 'bounds',
                     'retry_mismatch', 'input_mutation', 'nonprogress'):
            for status, code in (('error', 2), ('failed', 1)):
                with self.subTest(kind=kind, status=status):
                    self.assertFalse(mutations.is_detected({'status': status, 'exit_code': code,
                                                            'failure': {'kind': kind}}))
        for status, code in (('passed', 0), ('error', 2), ('failed', 2)):
            self.assertFalse(mutations.is_detected({'status': status, 'exit_code': code,
                                                    'failure': {'kind': 'row_mismatch'}}))
        self.assertFalse(mutations.is_detected({}))

    def test_invalid_or_ambiguous_edits_cannot_run_as_mutants(self):
        self.assertEqual(mutations.apply_mutation('x = 1\n', '1', '2'), 'x = 2\n')
        for source, before, after in (('x = 1\n', 'missing', '2'),
                                      ('x = 1\ny = 1\n', '1', '2'),
                                      ('x = 1\n', '1', '1')):
            with self.assertRaises(ValueError):
                mutations.apply_mutation(source, before, after)
        with self.assertRaises(SyntaxError):
            mutations.apply_mutation('x = 1\n', '1', '(')

    def test_failed_healthy_control_stops_before_any_mutant(self):
        source = (KIT / 'sample_pager.py').read_bytes()
        with patch.object(mutations, 'run_case', return_value={
            'status': 'error', 'exit_code': 2, 'failure': {'kind': 'timeout'}
        }) as worker:
            report = mutations.run_checks()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(report['mutations'], [])
        self.assertTrue(report['source_unchanged'])
        self.assertEqual((KIT / 'sample_pager.py').read_bytes(), source)

    def test_mutant_timeouts_do_not_pass_the_catalogue(self):
        counter = [0]
        def worker(*args, **kwargs):
            counter[0] += 1
            if counter[0] % 2:
                return {'status': 'passed', 'exit_code': 0}
            return {'status': 'error', 'exit_code': 2, 'failure': {'kind': 'timeout'}}
        with patch.object(mutations, 'run_case', side_effect=worker):
            report = mutations.run_checks()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(len(report['mutations']), 2 * len(mutations.MUTATIONS))
        self.assertFalse(any(value['detected'] for value in report['mutations']))
        self.assertTrue(report['source_unchanged'])


if __name__ == '__main__':
    unittest.main()
