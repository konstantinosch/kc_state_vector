"""The placement mutation tool must fail closed without editing real sources."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reference.mutations import Mutation
from reference.placement import mutations


TEST = "reference.placement.tests.example.ExampleTests.test_value"
CHANGE = Mutation("example.change", "Example value differs", "value = 1", "value = 2", (TEST,))


class MutationToolTests(unittest.TestCase):
    def package(self, temporary):
        package = Path(temporary) / "reference"
        (package / "placement").mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "placement/__init__.py").write_text("", encoding="utf-8")
        (package / "placement/protocol.py").write_bytes(b"value = 1\n")
        return package

    def test_catalogue_has_unique_names_and_expected_source_targets(self):
        mutations._validate_catalogue(mutations.CATALOGUE)
        self.assertEqual(tuple(map(len, (mutations.ONE_WAY, mutations.TWO_WAY,
                                       mutations.RECOMPUTE, mutations.CROSS_CHAIN))), (7, 12, 10, 2))
        targets = {mutation.name: target for target, mutation in mutations.CATALOGUE}
        self.assertEqual(targets["one_way.bypass_conditional_publication"], "protocol.py")
        self.assertEqual(targets["two_way.bypass_conditional_publication"], "protocol.py")
        self.assertEqual(targets["recompute.overwrite_concurrent_winner"], "recompute.py")

    def test_bad_catalogue_is_rejected_before_any_worker(self):
        invalid = ((), (("../protocol.py", CHANGE),), (("protocol.py", CHANGE),) * 2,
                   (("protocol.py", Mutation("empty", "Empty tests", "1", "2", ())),),
                   (("protocol.py", Mutation("private", "Wrong package", "1", "2", ("outside.test",))),))
        with tempfile.TemporaryDirectory() as temporary:
            package = self.package(temporary)
            for catalogue in invalid:
                with self.subTest(catalogue=catalogue), patch.object(mutations, "_run_tests") as worker:
                    report = mutations.run_checks(reference_root=package, catalogue=catalogue)
                    self.assertEqual(report["status"], "failed")
                    worker.assert_not_called()

    def test_stale_edit_does_not_count_as_a_detected_fault(self):
        stale = Mutation("stale", "Missing edit anchor", "not present", "different", (TEST,))
        with tempfile.TemporaryDirectory() as temporary:
            package = self.package(temporary)
            with patch.object(mutations, "_run_tests") as worker:
                report = mutations.run_checks(reference_root=package, catalogue=(("protocol.py", stale),))
            self.assertEqual(report["status"], "failed")
            self.assertIn("expected one source match", report["detail"])
            worker.assert_not_called()
            self.assertEqual((package / "placement/protocol.py").read_bytes(), b"value = 1\n")

    def test_failed_control_prevents_mutant_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = self.package(temporary)
            for status in ("baseline_failed", "infrastructure_error", "timeout"):
                with self.subTest(status=status), patch.object(
                    mutations, "_run_tests", return_value={"status": status}
                ) as worker:
                    report = mutations.run_checks(reference_root=package, catalogue=(("protocol.py", CHANGE),))
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["mutations"], [])
                self.assertTrue(report["source_unchanged"])
                self.assertEqual(worker.call_count, 1)

    def test_survivor_error_or_timeout_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = self.package(temporary)
            for status in ("survived", "infrastructure_error", "timeout"):
                with self.subTest(status=status), patch.object(mutations, "_run_tests", side_effect=[
                    {"status": "passed"}, {"status": status},
                ]):
                    report = mutations.run_checks(reference_root=package, catalogue=(("protocol.py", CHANGE),))
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["mutations"][0]["status"], status)
                self.assertTrue(report["source_unchanged"])

    def test_mutation_touches_only_the_temporary_public_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = self.package(temporary)
            private = Path(temporary) / "private-notes.py"
            private.write_text("PRIVATE = True\n", encoding="utf-8")
            results = package / "placement/results"
            results.mkdir()
            (results / "old.py").write_text("HISTORICAL = True\n", encoding="utf-8")
            calls = []

            def worker(root, tests, timeout, *, mutant):
                self.assertEqual(tests, (TEST,))
                self.assertFalse((root / "local_reference").exists())
                self.assertFalse((root / "private-notes.py").exists())
                self.assertFalse((root / "reference/placement/results").exists())
                expected = "value = 2" if mutant else "value = 1"
                self.assertEqual((root / "reference/placement/protocol.py").read_text().strip(), expected)
                calls.append((root, mutant))
                return {"status": "detected" if mutant else "passed"}

            with patch.object(mutations, "_run_tests", side_effect=worker):
                report = mutations.run_checks(reference_root=package, catalogue=(("protocol.py", CHANGE),))
            self.assertEqual(report["status"], "passed")
            self.assertTrue(report["source_unchanged"])
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(calls[0][0], calls[1][0])
            self.assertTrue(all(not root.exists() for root, _ in calls))
            self.assertEqual((package / "placement/protocol.py").read_bytes(), b"value = 1\n")
            self.assertEqual(private.read_text(), "PRIVATE = True\n")

    def test_invalid_timeout_is_rejected_without_workers(self):
        with patch.object(mutations, "_run_tests") as worker:
            for timeout in (0, -1, float("nan"), float("inf")):
                with self.subTest(timeout=timeout):
                    report = mutations.run_checks(timeout=timeout)
                    self.assertEqual(report["status"], "failed")
            worker.assert_not_called()

    def test_cli_rejects_source_output_and_failed_report(self):
        with patch.object(mutations, "run_checks") as runner, redirect_stderr(io.StringIO()):
            for target in (mutations.REFERENCE_ROOT / "model.py",
                           mutations.REFERENCE_ROOT / "placement/protocol.py"):
                with self.subTest(target=target), self.assertRaises(SystemExit) as raised:
                    mutations.main(["--json", str(target)])
                self.assertEqual(raised.exception.code, 2)
            runner.assert_not_called()
        report = {"status": "failed", "control": {"status": "passed"},
                  "mutations": [{"name": "survivor", "status": "survived"}]}
        with patch.object(mutations, "run_checks", return_value=report), redirect_stdout(io.StringIO()):
            self.assertEqual(mutations.main([]), 1)


if __name__ == "__main__":
    unittest.main()
