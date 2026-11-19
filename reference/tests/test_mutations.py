"""Tooling failures must not masquerade as detected behavioral faults."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reference.mutations import Mutation, apply_mutation, classify, main, run_checks


def result(**updates):
    value = dict(tests_run=1, failures=0, errors=0, skipped=0,
                 expected_failures=0, unexpected_successes=0)
    value.update(updates)
    return value


class MutationToolTests(unittest.TestCase):
    def test_source_edit_must_match_once_and_compile(self):
        mutation = Mutation("change", "test", "value = 1", "value = 2", ("test",))
        self.assertEqual(apply_mutation("value = 1\n", mutation), "value = 2\n")
        for source in ("value = 0\n", "value = 1\nvalue = 1\n"):
            with self.assertRaisesRegex(ValueError, "expected one"):
                apply_mutation(source, mutation)
        with self.assertRaises(SyntaxError):
            apply_mutation("value = 1", Mutation("bad", "test", "1", "(", ("test",)))
        with self.assertRaisesRegex(ValueError, "does not change"):
            apply_mutation("value = 1", Mutation("noop", "test", "1", "1", ("test",)))

    def test_only_assertion_failures_count_as_detection(self):
        self.assertEqual(classify(result(failures=1), 1, mutant=True), "detected")
        self.assertEqual(classify(result(), 1, mutant=True), "survived")
        self.assertEqual(classify(result(), 1, mutant=False), "passed")
        self.assertEqual(classify(result(failures=1), 1, mutant=False), "baseline_failed")
        for change in (dict(errors=1), dict(skipped=1), dict(expected_failures=1),
                       dict(unexpected_successes=1), dict(tests_run=0), dict(errors="0")):
            with self.subTest(change=change):
                self.assertEqual(classify(result(failures=1, **change), 1, mutant=True),
                                 "infrastructure_error")

    def test_survivor_fails_run_without_editing_original(self):
        mutation = Mutation("change", "test", "value = 1", "value = 2", ("test",))
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            model = package / "model.py"
            model.write_bytes(b"value = 1\n")
            with patch("reference.mutations._run_tests", side_effect=[
                {"status": "passed"}, {"status": "survived"},
            ]):
                report = run_checks(package=package, mutations=(mutation,))
            self.assertEqual(report["status"], "failed")
            self.assertTrue(report["source_unchanged"])
            self.assertEqual(model.read_bytes(), b"value = 1\n")
            self.assertEqual(list(package.iterdir()), [model])

    def test_stale_catalogue_fails_before_tests(self):
        mutation = Mutation("stale", "test", "missing", "different", ("test",))
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            (package / "model.py").write_bytes(b"value = 1\n")
            with patch("reference.mutations._run_tests") as worker:
                report = run_checks(package=package, mutations=(mutation,))
            self.assertEqual(report["status"], "failed")
            worker.assert_not_called()

    def test_failed_control_does_not_test_mutants(self):
        mutation = Mutation("change", "test", "value = 1", "value = 2", ("test",))
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            (package / "model.py").write_bytes(b"value = 1\n")
            with patch("reference.mutations._run_tests", return_value={"status": "baseline_failed"}) as worker:
                report = run_checks(package=package, mutations=(mutation,))
            self.assertEqual(report["status"], "failed")
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(report["mutations"], [])

    def test_cli_exit_code_rejects_survivors(self):
        report = {"status": "failed", "baseline": {"status": "passed"},
                  "mutations": [{"name": "example", "status": "survived"}]}
        with patch("reference.mutations.run_checks", return_value=report), redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 1)

    def test_report_cannot_overwrite_reference_source(self):
        target = Path(__file__).resolve().parents[1] / "model.py"
        before = target.read_bytes()
        with patch("reference.mutations.run_checks") as runner, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["--json", str(target)])
        self.assertEqual(raised.exception.code, 2)
        runner.assert_not_called()
        self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
