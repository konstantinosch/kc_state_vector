"""Public placement sources can be imported without private research or site paths."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from reference.mutations import _copy_package


REFERENCE_ROOT = Path(__file__).resolve().parents[2]


class PublicPlacementExportTests(unittest.TestCase):
    def test_exported_sources_match_the_recorded_hashes(self):
        package = REFERENCE_ROOT / "placement"
        manifest = json.loads((package / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(set(manifest["files"]),
                         {path.relative_to(package).as_posix() for path in package.rglob("*.py")})
        for relative, entry in manifest["files"].items():
            with self.subTest(path=relative):
                path = Path(relative)
                self.assertFalse(path.is_absolute())
                self.assertNotIn("..", path.parts)
                self.assertEqual(hashlib.sha256((package / path).read_bytes()).hexdigest(),
                                 entry["sha256"])

    def test_placement_has_no_private_imports(self):
        forbidden = ("local_reference", "pagerbench", "benchmarks", "review_checks")
        for source in (REFERENCE_ROOT / "placement").rglob("*.py"):
            if any(part in ("results", "runs", "__pycache__") for part in source.parts):
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                for name in names:
                    with self.subTest(source=source.name, name=name):
                        self.assertFalse(any(name == prefix or name.startswith(prefix + ".")
                                             for prefix in forbidden))

    def test_clean_copy_imports_without_local_environment(self):
        with tempfile.TemporaryDirectory(prefix="kc-placement-export-") as temporary:
            root = Path(temporary)
            _copy_package(REFERENCE_ROOT, root)
            environment = dict(os.environ)
            for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE"):
                environment.pop(name, None)
            command = [sys.executable, "-S", "-B", "-c",
                       "import reference.placement.protocol; "
                       "import reference.placement.two_way; "
                       "import reference.placement.recompute; "
                       "import reference.placement.adapters; "
                       "import reference.placement.mutations; "
                       "import sys; "
                       "assert not any(n == 'local_reference' or n.startswith('local_reference.') "
                       "for n in sys.modules); print('public placement imports passed')"]
            completed = subprocess.run(command, cwd=root, env=environment, timeout=300,
                                       capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("public placement imports passed", completed.stdout)
            self.assertFalse((root / "local_reference").exists())

    def test_optimized_entrypoints_inherit_the_core_guard(self):
        environment = dict(os.environ)
        for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE"):
            environment.pop(name, None)
        for flag in ("-O", "-OO"):
            for arguments in (("-c", "import reference.placement"),
                              ("-m", "reference.placement.comparison", "--help"),
                              ("-m", "reference.placement.mutations", "--help")):
                with self.subTest(flag=flag, arguments=arguments):
                    completed = subprocess.run(
                        [sys.executable, "-X", "utf8", "-S", "-B", flag, *arguments],
                        cwd=REFERENCE_ROOT.parent, env=environment, timeout=300,
                        capture_output=True, text=True, encoding="utf-8")
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn("reference requires assertions enabled.", completed.stderr)
                    self.assertEqual(completed.stdout, "")


if __name__ == "__main__":
    unittest.main()
