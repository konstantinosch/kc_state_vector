"""Independent fixture expectations and behavior of the bundled B3 example pager."""

import ast
import copy
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / 'skills/kc-state-vector'
SCRIPTS = PACKAGE / 'scripts'


def load_module(name):
    spec = importlib.util.spec_from_file_location('b3_test_' + name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


oracle = load_module('oracle')
sample = load_module('sample_pager')
template = load_module('template_pager')


EXPECTED = {
    'priority_51_49_48': [('priority', '51'), ('priority', '49'), ('all', '52'), ('all', '50'), ('all', '48')],
    'full_tuple_ties': [('priority', 'z'), ('priority', 'm'), ('all', 'a'), ('all', 'negative')],
    'decimal_id_ties': [('all', '2'), ('all', '10')],
    'empty_duplicate_page': [('first', 'A'), ('first', 'B'), ('all', 'C')],
    'three_overlapping_stages': [('first', 'A'), ('first', 'B'), ('middle', 'C'), ('rest', 'D'), ('rest', 'E'), ('rest', 'F')],
    'unread_batch_tail': [('all', 'A'), ('all', 'B'), ('all', 'C'), ('all', 'D')],
    'empty_plan': [], 'empty_sources': [], 'zero_quotas': [('all', 'A'), ('all', 'B')], 'all_zero_quotas': [],
}


def fixture(name):
    return oracle.load_fixture(SCRIPTS / 'fixtures' / (name + '.json'))


def rows(pairs):
    return [{'stage': stage, 'id': identity} for stage, identity in pairs]


class SkillOracleTests(unittest.TestCase):
    def test_fixture_expectations_are_handwritten_and_complete(self):
        self.assertEqual(set(EXPECTED), {p.stem for p in (SCRIPTS / 'fixtures').glob('*.json')})
        for name, expected in EXPECTED.items():
            with self.subTest(fixture=name):
                data = fixture(name)
                original = copy.deepcopy(data)
                self.assertIs(oracle.validate_fixture(data), data)
                self.assertEqual(oracle.unpaged_oracle(data), rows(expected))
                self.assertEqual(data, original)

    def test_readable_expected_table_matches_independent_expectations(self):
        text = (PACKAGE / 'references/self-check-examples.md').read_text(encoding='utf-8')
        table = dict(re.findall(r'^\| \[([^]]+)\]\([^)]*\) \| `([^`]+)` \|$', text, re.M))
        self.assertEqual(set(table), set(EXPECTED))
        for name, expected in EXPECTED.items():
            self.assertEqual(table[name], ', '.join(stage + ':' + identity for stage, identity in expected) or 'EMPTY')

    def test_oracle_has_no_runtime_pager_or_reference_import(self):
        tree = ast.parse((SCRIPTS / 'oracle.py').read_text(encoding='utf-8'))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module)
        self.assertEqual(set(imports), {'json', 'pathlib'})

    def test_record_and_membership_order_do_not_define_selection(self):
        data = fixture('three_overlapping_stages')
        expected = oracle.unpaged_oracle(data)
        data['records'].reverse()
        for stage in data['stages']:
            stage['ids'].reverse()
        self.assertEqual(oracle.unpaged_oracle(data), expected)

    def test_invalid_fixture_fields_and_types_are_rejected(self):
        valid = fixture('priority_51_49_48')
        variants = []
        for field, bad in [('schema_version', True), ('schema_version', 2), ('name', ''), ('name', '\u03bb'),
                           ('records', ()), ('stages', {})]:
            data = copy.deepcopy(valid)
            data[field] = bad
            variants.append(data)
        data = copy.deepcopy(valid)
        data['expected'] = []
        variants.append(data)
        for value in variants:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    oracle.validate_fixture(value)

    def test_conflicting_ids_unknown_members_and_duplicate_members_fail(self):
        for operation in ('duplicate_record', 'unknown_member', 'duplicate_member', 'duplicate_stage'):
            data = fixture('priority_51_49_48')
            if operation == 'duplicate_record':
                data['records'].append({'id': '51', 'sort_key': 999})
            elif operation == 'unknown_member':
                data['stages'][0]['ids'].append('missing')
            elif operation == 'duplicate_member':
                data['stages'][0]['ids'].append('51')
            else:
                data['stages'][1]['name'] = data['stages'][0]['name']
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                oracle.validate_fixture(data)

    def test_integer_profiles_reject_boolean_fraction_and_out_of_range(self):
        for field, values in [('sort_key', [True, 1.5, 2**53, -(2**53)]),
                              ('quota', [False, 1.5, -1, 10001])]:
            for value in values:
                data = fixture('priority_51_49_48')
                target = data['records'][0] if field == 'sort_key' else data['stages'][0]
                target[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    oracle.validate_fixture(data)

    def test_loader_rejects_duplicate_fields_nonfinite_and_oversize(self):
        good = json.dumps(fixture('empty_plan'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.json'
            for text in [good.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'),
                         good.replace('"schema_version": 1', '"schema_version": NaN'),
                         good.replace('"schema_version": 1', '"schema_version": Infinity')]:
                path.write_text(text, encoding='utf-8')
                with self.subTest(text=text), self.assertRaises(ValueError):
                    oracle.load_fixture(path)
            path.write_bytes(b' ' * (oracle.MAX_FIXTURE_BYTES + 1))
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                oracle.load_fixture(path)

    def test_fixture_dimension_limits_are_checked_before_content(self):
        for field, size in [('records', oracle.MAX_RECORDS), ('stages', oracle.MAX_STAGES)]:
            data = fixture('empty_plan')
            data[field] = [None] * (size + 1)
            with self.subTest(field=field), self.assertRaises(ValueError):
                oracle.validate_fixture(data)

    def test_template_cannot_silently_pass(self):
        self.assertEqual(template.CAPABILITIES, set())
        with self.assertRaises(NotImplementedError):
            template.make_pager(fixture('empty_plan'))(None, page_size=1)

    def test_sample_matches_handwritten_outputs_under_varied_schedules(self):
        schedules = [((1,), (1,), (1,)), ((3,), (3,), (50,)), ((50,), (50,), (1000,)),
                     ((1, 3, 7, 50), (3, 1, 50), (1, 2, 50))]
        for name, expected in EXPECTED.items():
            data = fixture(name)
            original = copy.deepcopy(data)
            for sizes, batches, budgets in schedules:
                with self.subTest(fixture=name, sizes=sizes, batches=batches, budgets=budgets):
                    state, actual = None, []
                    for n in range(sum(len(s['ids']) for s in data['stages']) + len(data['stages']) + 1):
                        before = copy.deepcopy(state)
                        limits = dict(page_size=sizes[n % len(sizes)], batch_size=batches[n % len(batches)],
                                      candidate_budget=budgets[n % len(budgets)])
                        page = sample.make_pager(data)(state, **limits)
                        replay = sample.make_pager(data)(copy.deepcopy(before), **limits)
                        self.assertEqual(page, replay)
                        self.assertEqual(state, before)
                        self.assertEqual(data, original)
                        self.assertLessEqual(len(page['rows']), limits['page_size'])
                        self.assertLessEqual(page['consumed'], limits['candidate_budget'])
                        if page['continuation'] is not None and len(page['rows']) < limits['page_size']:
                            self.assertEqual(page['consumed'], limits['candidate_budget'])
                        actual.extend(page['rows'])
                        if page['continuation'] is None:
                            break
                        state = json.loads(json.dumps(page['continuation']))
                    else:
                        self.fail('Sample did not terminate')
                    self.assertEqual(actual, rows(expected))

    def test_sample_empty_pages_have_successors(self):
        data = fixture('empty_duplicate_page')
        state, pages = None, []
        for budget in (2, 1, 1, 1):
            page = sample.make_pager(data)(state, page_size=2, batch_size=3, candidate_budget=budget)
            pages.append(page)
            state = page['continuation']
        self.assertEqual([[row['id'] for row in page['rows']] for page in pages], [['A', 'B'], [], [], ['C']])
        self.assertTrue(all(page['continuation'] is not None for page in pages[:3]))
        self.assertIsNone(pages[-1]['continuation'])

    def test_sample_retires_only_after_duplicate_decision(self):
        data = fixture('empty_duplicate_page')
        first = sample.make_pager(data)(None, page_size=2, batch_size=3, candidate_budget=2)
        self.assertEqual(first['continuation']['coverage'], ['A', 'B'])
        second = sample.make_pager(data)(first['continuation'], page_size=2, batch_size=3, candidate_budget=1)
        self.assertEqual(second['rows'], [])
        self.assertEqual(second['continuation']['coverage'], ['B'])

    def test_sample_keeps_unread_batch_tail(self):
        data = fixture('unread_batch_tail')
        first = sample.make_pager(data)(None, page_size=1, batch_size=3, candidate_budget=50)
        second = sample.make_pager(data)(first['continuation'], page_size=1, batch_size=3, candidate_budget=50)
        self.assertEqual(first['rows'], rows([('all', 'A')]))
        self.assertEqual(second['rows'], rows([('all', 'B')]))

    def test_sample_rejects_invalid_limits_and_foreign_fixture(self):
        data = fixture('priority_51_49_48')
        page = sample.make_pager(data)
        for value in (0, -1, True, 1.5, 1001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                page(None, page_size=value)
        first = page(None, page_size=1)
        changed = copy.deepcopy(data)
        changed['stages'][0]['quota'] = 1
        with self.assertRaisesRegex(ValueError, 'different fixture'):
            sample.make_pager(changed)(first['continuation'], page_size=1)


if __name__ == '__main__':
    unittest.main()
