"""Publication integrity checks, separate from the semantic mutation suite.

These replace four local frozen-archive lifecycle tests in the public export.
The existing 74 semantic/report tests remain present. Hash failures do not count
as detecting a deliberately broken pager; mutation checks use semantic tests.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
OPTIMIZATION_MODES = ((('-O',), None), (('-OO',), None), ((), '1'), ((), '2'))
OPTIMIZATION_ERROR = (
    "reference requires assertions enabled. "
    "Run Python without -O/-OO and unset PYTHONOPTIMIZE."
)


def provenance():
    return json.loads((ROOT / "provenance.json").read_text(encoding="utf-8"))


class PublicExportTests(unittest.TestCase):
    def _python(self, arguments, *, flags=(), optimize=None):
        environment = dict(os.environ)
        for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONOPTIMIZE'):
            environment.pop(key, None)
        if optimize is not None:
            environment['PYTHONOPTIMIZE'] = optimize
        return subprocess.run(
            [sys.executable, '-X', 'utf8', '-S', '-B', *flags, *arguments],
            cwd=ROOT.parent, env=environment, capture_output=True,
            text=True, encoding='utf-8', timeout=300,
        )

    def test_optimized_import_is_rejected_before_loading_model(self):
        program = '''
import sys

class BlockModelImport:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'reference.model':
            raise AssertionError('MODEL_IMPORT_BEFORE_OPTIMIZATION_GUARD')

sys.meta_path.insert(0, BlockModelImport())
import reference.model
print('UNEXPECTED_SUCCESS')
'''
        for flags, optimize in OPTIMIZATION_MODES:
            with self.subTest(flags=flags, optimize=optimize):
                result = self._python(['-c', program], flags=flags, optimize=optimize)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(OPTIMIZATION_ERROR, result.stderr)
                self.assertNotIn('MODEL_IMPORT_BEFORE_OPTIMIZATION_GUARD', result.stderr)
                self.assertEqual(result.stdout, '')

    def test_optimized_cli_and_validation_runners_are_rejected(self):
        for module in ('reference', 'reference.exhaustive', 'reference.closing',
                       'reference.long_traversals', 'reference.mutations'):
            for flags, optimize in OPTIMIZATION_MODES:
                with self.subTest(module=module, flags=flags, optimize=optimize):
                    result = self._python(['-m', module, '--help'],
                                          flags=flags, optimize=optimize)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(OPTIMIZATION_ERROR, result.stderr)
                    self.assertEqual(result.stdout, '')

    def test_unoptimized_prepared_pages_still_resume(self):
        program = '''
from reference import Item, Pager, Plan, Stage

pager = Pager(Plan((Stage('prepared', (Item('a', 2), Item('b', 1)), buffer_size=2),)))
first = pager.page(page_size=1, batch_size=2)
if first.ids != ('a',) or first.state['buffers'][0]['items'] != ['b']:
    raise AssertionError('first page did not consume its prepared item')
second = pager.page(first.state, page_size=1)
if second.ids != ('b',) or second.state is not None:
    raise AssertionError('prepared continuation did not finish correctly')
print('prepared resume passed')
'''
        for optimize in (None, '0'):
            with self.subTest(optimize=optimize):
                result = self._python(['-c', program], optimize=optimize)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'prepared resume passed')

    def test_exported_files_match_the_recorded_source_hashes(self):
        manifest = provenance()
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["baseline"]["id"], "core-reservations-v1")
        self.assertEqual(manifest["baseline"]["archive_sha256"],
                         "72976ba68f7920f71955746a1151bf9edf82692c5dd8f3b597f0e41d37ba9607")
        self.assertTrue(manifest["files"])
        for relative, entry in manifest["files"].items():
            with self.subTest(path=relative):
                path = Path(relative)
                self.assertFalse(path.is_absolute())
                self.assertNotIn("..", path.parts)
                self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), entry["sha256"])

    def test_semantic_implementation_and_oracle_are_unchanged_from_origin(self):
        files = provenance()["files"]
        for name in ("model.py", "oracle.py", "retirement.py", "capacity_checks.py",
                     "failure_checks.py", "long_scenarios.py", "tiny_checker.py", "tiny_plans.py"):
            with self.subTest(path=name):
                self.assertEqual(files[name]["origin"], "local_reference/" + name)
                self.assertEqual(files[name]["origin_sha256"], files[name]["sha256"])
                self.assertEqual(files[name]["adaptations"], [])

    def test_vendored_legacy_helpers_keep_the_original_function_bodies(self):
        entry = provenance()["files"]["tests/legacy_helpers.py"]
        source = (ROOT / "tests/legacy_helpers.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertFalse(any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree)))
        functions = {node.name: ast.get_source_segment(source, node)
                     for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertEqual(set(functions), {"stage", "prepared_buffer_model", "unpaged_staged_oracle"})
        for name, body in functions.items():
            self.assertEqual(hashlib.sha256(body.encode("utf-8")).hexdigest(),
                             entry["extracted_functions"][name], name)

    def test_oracle_executes_without_importing_the_pager_or_local_research(self):
        program = '''
import importlib.util
import sys
from types import SimpleNamespace as NS

class NoImplementationImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'reference', 'local_reference', 'review_checks', 'mailbench'}:
            raise AssertionError('oracle imported implementation: ' + fullname)

sys.meta_path.insert(0, NoImplementationImports())
spec = importlib.util.spec_from_file_location('isolated_unpaged_oracle', sys.argv[1])
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)
records = {str(n): NS(id=str(n), sort_key=n) for n in range(48, 53)}
plan = NS(stages=(NS(name='priority', items=[records[n] for n in ('51', '49', '48')], quota=2),
                  NS(name='rest', items=list(records.values()), quota=None)), reservations=())
expected = [('priority', '51'), ('priority', '49'), ('rest', '52'), ('rest', '50'), ('rest', '48')]
assert oracle.unpaged_oracle(plan) == expected
print('independent oracle passed')
'''
        result = subprocess.run([sys.executable, "-I", "-S", "-c", program, str(ROOT / "oracle.py")],
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), "independent oracle passed")


if __name__ == "__main__":
    unittest.main()
