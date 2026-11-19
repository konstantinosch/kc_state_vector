"""Public documentation checks, exercised only against disposable fixtures."""

from pathlib import Path
import tempfile
import unittest

from scripts.check_docs import check_repository


BUILDER = Path(__file__).resolve().parents[2] / "skills" / "build_skill.py"
PATTERN = "# Pattern\n\n## Core\n\n```json\n{\"page_size\": 3}\n```\n"
SKILL = """---
name: kc-state-vector
description: Explain state-vector pagination.
license: MIT
metadata:
  author: Example Author
  version: "1.0"
---

# State-vector pagination

Read the [pattern](references/kc-state-vector.md#core).
"""


class CheckDocsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.write("README.md", "# Public documentation\n\n[Core](kc-state-vector.md#core)\n")
        self.write("kc-state-vector.md", PATTERN)
        self.write("LICENSE", "MIT License\n\nCopyright Example Author\n")
        self.write("skills/build_skill.py", BUILDER.read_text(encoding="utf-8"))
        self.write("skills/kc-state-vector/SKILL.md", SKILL)
        self.write("skills/kc-state-vector/references/kc-state-vector.md", PATTERN)
        self.write("skills/kc-state-vector/LICENSE", (self.root / "LICENSE").read_text())

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def write_pattern(self, content):
        self.write("kc-state-vector.md", content)
        self.write("skills/kc-state-vector/references/kc-state-vector.md", content)

    def snapshot(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def assert_rejected(self, *, build=False, mentions=None):
        errors = check_repository(self.root, build=build)
        self.assertTrue(errors, "The invalid fixture passed validation")
        if mentions is not None:
            self.assertIn(mentions.lower(), "\n".join(errors).lower())
        return errors

    def test_valid_public_documentation(self):
        self.assertEqual([], check_repository(self.root, build=False))

    def test_missing_local_target_and_fragment_are_rejected(self):
        for target in ("missing.md", "kc-state-vector.md#missing-section"):
            with self.subTest(target=target):
                self.write("README.md", f"# Documentation\n\n[Read more]({target})\n")
                self.assert_rejected(mentions="README.md")

    def test_code_fences_hide_links_and_headings(self):
        self.write(
            "README.md",
            "# Documentation\n\n```markdown\n## Pretend heading\n"
            "[Example](missing.md#missing)\n```\n",
        )
        self.assertEqual([], check_repository(self.root, build=False))
        with (self.root / "README.md").open("a", encoding="utf-8") as file:
            file.write("\n[Not a real heading](#pretend-heading)\n")
        self.assert_rejected(mentions="pretend-heading")

    def test_duplicate_heading_suffixes_resolve(self):
        self.write_pattern(PATTERN + "\n## Repeated\n\nFirst.\n\n## Repeated\n\nSecond.\n")
        self.write(
            "README.md",
            "# Documentation\n\n[First](kc-state-vector.md#repeated)\n"
            "[Second](kc-state-vector.md#repeated-1)\n",
        )
        self.assertEqual([], check_repository(self.root, build=False))

    def test_malformed_and_nonfinite_json_are_rejected(self):
        for payload in (
            '{"value": }',
            '{"value": NaN}',
            '{"value": Infinity}',
            '{"value": -Infinity}',
        ):
            with self.subTest(payload=payload):
                self.write_pattern(f"# Pattern\n\n## Core\n\n```json\n{payload}\n```\n")
                self.assert_rejected(mentions="json")

    def test_valid_json_numbers_can_exceed_python_float_range(self):
        self.write_pattern('# Pattern\n\n## Core\n\n```json\n{"large": 1e999, "small": -1e999}\n```\n')
        self.assertEqual([], check_repository(self.root, build=False))

    def test_unclosed_code_fence_is_rejected(self):
        self.write_pattern("# Pattern\n\n## Core\n\n```json\n{\"value\": 1}\n")
        self.assert_rejected(mentions="fence")

    def test_invalid_skill_front_matter_is_rejected(self):
        invalid_skills = {
            "wrong name": SKILL.replace("name: kc-state-vector", "name: unrelated-skill"),
            "empty description": SKILL.replace(
                "description: Explain state-vector pagination.", 'description: ""'
            ),
            "long description": SKILL.replace(
                "description: Explain state-vector pagination.", "description: " + "x" * 201
            ),
            "malformed YAML": SKILL.replace("license: MIT", "license: [MIT"),
            "nonstring author": SKILL.replace("author: Example Author", "author: 123"),
            "nonstring version": SKILL.replace('version: "1.0"', "version: 1.0"),
        }
        for case, content in invalid_skills.items():
            with self.subTest(case=case):
                self.write("skills/kc-state-vector/SKILL.md", content)
                self.assert_rejected(mentions="SKILL.md")

    def test_stale_bundled_inputs_are_rejected_without_repairing_sources(self):
        for relative in (
            "skills/kc-state-vector/references/kc-state-vector.md",
            "skills/kc-state-vector/LICENSE",
        ):
            with self.subTest(relative=relative):
                original = (self.root / relative).read_bytes()
                self.write(relative, "Stale bundled content.\n")
                before = self.snapshot()
                self.assert_rejected(build=True, mentions=relative)
                self.assertEqual(before, self.snapshot(), "Validation modified the source repository")
                (self.root / relative).write_bytes(original)

    def test_existing_private_document_is_not_a_public_link_target(self):
        self.write("local_reference/notes.md", "# Private fixture\n")
        self.write("README.md", "# Documentation\n\n[Notes](local_reference/notes.md)\n")
        self.assert_rejected(mentions="local_reference/notes.md")

    def test_build_uses_only_public_inputs_and_leaves_sources_unchanged(self):
        self.write("unpublished-notes.txt", "This fixture must not enter the build directory.\n")
        builder = (self.root / "skills/build_skill.py").read_text(encoding="utf-8")
        self.write(
            "skills/build_skill.py",
            builder
            + '\nroot = Path(__file__).resolve().parent.parent\n'
            + 'if (root / "unpublished-notes.txt").exists():\n'
            + '    raise SystemExit("Build copied an unpublished input")\n',
        )
        before = self.snapshot()
        self.assertEqual([], check_repository(self.root, build=True))
        self.assertEqual(before, self.snapshot(), "Validation modified the source repository")

    def test_crlf_public_sources_build_successfully(self):
        for path in self.root.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        before = self.snapshot()
        self.assertTrue(all(b"\r\n" in content for content in before.values()))
        self.assertEqual([], check_repository(self.root, build=True))
        self.assertEqual(before, self.snapshot(), "Validation modified CRLF source files")

    def test_archive_with_an_extra_member_is_rejected(self):
        builder = (self.root / "skills/build_skill.py").read_text(encoding="utf-8")
        self.write(
            "skills/build_skill.py",
            builder
            + '\nroot = Path(__file__).resolve().parent.parent\n'
            + 'with zipfile.ZipFile(root / "dist" / "kc-state-vector.zip", "a") as archive:\n'
            + '    archive.writestr("kc-state-vector/extra.txt", "Unexpected public package member")\n',
        )
        self.assert_rejected(build=True)

    def test_nondeterministic_archive_is_rejected(self):
        builder = (self.root / "skills/build_skill.py").read_text(encoding="utf-8")
        self.write(
            "skills/build_skill.py",
            builder
            + '\nimport uuid\n'
            + 'root = Path(__file__).resolve().parent.parent\n'
            + 'with zipfile.ZipFile(root / "dist" / "kc-state-vector.zip", "a") as archive:\n'
            + '    archive.comment = uuid.uuid4().hex.encode("ascii")\n',
        )
        self.assert_rejected(build=True)

    def test_archive_depending_on_source_mtimes_is_rejected(self):
        builder = (self.root / "skills/build_skill.py").read_text(encoding="utf-8")
        self.write(
            "skills/build_skill.py",
            builder
            + '\nroot = Path(__file__).resolve().parent.parent\n'
            + 'package = root / "skills" / "kc-state-vector"\n'
            + 'with zipfile.ZipFile(root / "dist" / "kc-state-vector.zip", "w") as archive:\n'
            + '    for relative in ("SKILL.md", "references/kc-state-vector.md", "LICENSE"):\n'
            + '        archive.write(package / relative, arcname="kc-state-vector/" + relative)\n',
        )
        self.assert_rejected(build=True, mentions="reproducible")


if __name__ == "__main__":
    unittest.main()
