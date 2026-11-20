"""Check public documentation and the portable skill, without reading local research.

Install requirements-checks.txt, then run this script from any working directory.
The checker validates source copies BEFORE running a builder in an isolated tree.
It checks local Markdown links, not the availability of external websites.
"""

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import argparse
import collections
import html
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import zipfile

try:
    import yaml
except ImportError:
    raise SystemExit("Install checker dependencies: python -m pip install -r requirements-checks.txt")


ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "skills/kc-state-vector"
SKILL_MARKDOWN_FILES = ("README.md", "kc-state-vector.md", f"{PACKAGE}/SKILL.md",
                        f"{PACKAGE}/references/kc-state-vector.md")
SKILL_BUILD_FILES = (*SKILL_MARKDOWN_FILES, "LICENSE", f"{PACKAGE}/LICENSE", "skills/build_skill.py")
REFERENCE_MARKDOWN_FILES = ("reference/README.md", "reference/VALIDATION.md",
                            "reference/grouped/README.md", "reference/grouped/VALIDATION.md")
# Deliberate publication boundary: never discover files recursively from the local
# workspace or allow an arbitrary file merely because it sits under reference/.
CORE_REFERENCE_SOURCE_FILES = tuple(f"reference/{name}" for name in (
    "__init__.py", "__main__.py", "model.py", "oracle.py", "retirement.py",
    "capacity_checks.py", "failure_checks.py", "closing.py", "long_scenarios.py",
    "long_traversals.py", "tiny_plans.py", "tiny_checker.py", "exhaustive.py",
    "mutations.py", "provenance.json", "tests/__init__.py", "tests/legacy_helpers.py",
    "tests/test_boundaries.py", "tests/test_capacity_closure.py",
    "tests/test_exhaustive_reporting.py", "tests/test_failure_closure.py",
    "tests/test_long_traversals.py", "tests/test_reference.py",
    "tests/test_retirement.py", "tests/test_tiny_checker.py", "tests/test_tiny_plans.py",
    "tests/test_public_export.py", "tests/test_mutations.py",
))
GROUPED_REFERENCE_SOURCE_FILES = tuple(f"reference/grouped/{name}" for name in (
    "__init__.py", "__main__.py", "audit.py", "coverage.py", "generated.py",
    "hybrid_scenarios.py", "model.py", "mutations.py", "oracle.py", "reporting.py",
    "run_checks.py", "run_hybrid_checks.py", "scenarios.py", "provenance.json",
    "tests/__init__.py", "tests/test_audit.py", "tests/test_contract.py",
    "tests/test_coverage.py", "tests/test_hybrid_audit.py", "tests/test_hybrid_coverage.py",
    "tests/test_hybrid_pager.py", "tests/test_pager.py", "tests/test_reporting.py",
    "tests/test_scenarios.py", "tests/test_public_export.py", "tests/test_mutations.py",
    "tests/test_public_boundaries.py",
))
REFERENCE_SOURCE_FILES = (*CORE_REFERENCE_SOURCE_FILES, *GROUPED_REFERENCE_SOURCE_FILES)
MARKDOWN_FILES = (*SKILL_MARKDOWN_FILES, *REFERENCE_MARKDOWN_FILES)
PUBLIC_FILES = (*SKILL_BUILD_FILES, *REFERENCE_MARKDOWN_FILES, *REFERENCE_SOURCE_FILES)
PUBLIC_SUPPORT_FILES = (".gitignore", ".gitattributes", "requirements-checks.txt",
                        "scripts/check_docs.py", "scripts/tests/test_check_docs.py",
                        ".github/workflows/checks.yml")
ARCHIVE_FILES = {"kc-state-vector/SKILL.md": f"{PACKAGE}/SKILL.md",
                 "kc-state-vector/references/kc-state-vector.md": f"{PACKAGE}/references/kc-state-vector.md",
                 "kc-state-vector/LICENSE": f"{PACKAGE}/LICENSE"}


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate metadata keys instead of silently accepting the last one."""


def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("Metadata keys must be unique strings.")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


@dataclass
class Document:
    anchors: set[str]
    links: list[tuple[int, str]]
    errors: list[str]


def _plain_heading(text):
    text = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"[^\w\-\s]", "", text.lower()).replace(" ", "-")


def _nonfinite(value):
    raise ValueError(f"Nonstandard JSON constant: {value}")


def _document(text):
    """Read the ATX headings, inline links and fenced examples used by this repo."""
    anchors = set()
    counts = collections.Counter()
    links, errors = [], []
    marker, fence_size, language, start = None, 0, "", 0
    code = []
    for number, line in enumerate(text.splitlines(), 1):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker is not None:
            if fence and fence[1][0] == marker and len(fence[1]) >= fence_size and not fence[2].strip():
                if language == "json":
                    try:
                        json.loads("\n".join(code), parse_constant=_nonfinite, parse_float=Decimal)
                    except (ValueError, RecursionError) as exc:
                        errors.append(f"line {start}: invalid JSON example: {exc}")
                marker, code = None, []
            else:
                code.append(line)
            continue
        if fence:
            marker, fence_size = fence[1][0], len(fence[1])
            language = fence[2].strip().split()[0].lower() if fence[2].strip() else ""
            start = number
            code = []
            continue
        heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$", line)
        if heading:
            base = _plain_heading(heading[1])
            slug = base if counts[base] == 0 else f"{base}-{counts[base]}"
            while slug in anchors:
                counts[base] += 1
                slug = f"{base}-{counts[base]}"
            counts[base] += 1
            anchors.add(slug)
        # Code examples can contain Markdown-looking text that is not a link.
        without_code = re.sub(r"(`+).*?\1", "", line)
        for match in re.finditer(r"!?\[[^\]]+\]\((<[^>]+>|[^\s)]+)(?:\s+\"[^\"]*\")?\)", without_code):
            links.append((number, match[1].strip("<>")))
    if marker is not None:
        errors.append(f"line {start}: unclosed fenced {language or 'code'} example")
    return Document(anchors, links, errors)


def _skill_metadata(text):
    match = re.match(r"\A---\n(.*?)\n---(?:\n|$)", text, re.S)
    if not match:
        return ["SKILL.md: missing or malformed YAML frontmatter"]
    try:
        metadata = yaml.load(match[1], Loader=UniqueLoader)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        return [f"SKILL.md: invalid YAML metadata: {exc}"]
    if not isinstance(metadata, dict):
        return ["SKILL.md: metadata must be a mapping"]
    errors = []
    if metadata.get("name") != "kc-state-vector":
        errors.append("SKILL.md: name must match the kc-state-vector package directory")
    description = metadata.get("description")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 200:
        errors.append("SKILL.md: description must contain 1-200 characters for this portable package")
    if metadata.get("license") != "MIT":
        errors.append("SKILL.md: license must match the bundled MIT license")
    extra = metadata.get("metadata", {})
    if not isinstance(extra, dict) or any(not isinstance(value, str) for value in extra.values()):
        errors.append("SKILL.md: metadata values must be strings")
    if len(text.splitlines()) > 500:
        errors.append("SKILL.md: keep the entry point below 500 lines; route detail to references")
    return errors


def _build_checks(root):
    errors = []
    with tempfile.TemporaryDirectory(prefix="kc-state-vector-check-") as folder:
        work = Path(folder)
        for relative in SKILL_BUILD_FILES:
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / relative, target)
        expected = {name: (root / path).read_bytes() for name, path in ARCHIVE_FILES.items()}
        outputs = []
        for attempt in range(2):
            try:
                # ZIP timestamps have two-second precision. Use different years
                # so an accidental dependency on source mtimes is observable.
                timestamp = 946684800 + attempt * 31536000
                for relative in SKILL_BUILD_FILES:
                    os.utime(work / relative, (timestamp, timestamp))
                process = subprocess.run([sys.executable, "-X", "utf8", str(work / "skills/build_skill.py")],
                                         cwd=work, capture_output=True, text=True, timeout=60)
                if process.returncode:
                    errors.append(f"Skill build {attempt + 1} failed: {process.stderr.strip() or process.stdout.strip()}")
                    break
                path = work / "dist/kc-state-vector.zip"
                outputs.append(path.read_bytes())
                with zipfile.ZipFile(path) as archive:
                    if collections.Counter(archive.namelist()) != collections.Counter(expected.keys()):
                        errors.append("Skill ZIP contains missing, duplicate or extra members")
                    elif archive.testzip() is not None:
                        errors.append("Skill ZIP integrity check failed")
                    else:
                        for name, content in expected.items():
                            if archive.read(name) != content:
                                errors.append(f"Skill ZIP payload differs from its source: {name}")
            except (OSError, subprocess.TimeoutExpired, zipfile.BadZipFile, RuntimeError) as exc:
                errors.append(f"Skill build/archive check failed: {exc}")
                break
        if len(outputs) == 2 and outputs[0] != outputs[1]:
            errors.append("Skill ZIP is not reproducible for identical source bytes")
    return errors


def check_repository(root, *, build=True):
    root = Path(root).resolve()
    errors = []
    for relative in PUBLIC_FILES:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            errors.append(f"Missing public file or unsupported symlink: {relative}")
    if errors:
        return errors
    for source, bundled in (("kc-state-vector.md", f"{PACKAGE}/references/kc-state-vector.md"),
                            ("LICENSE", f"{PACKAGE}/LICENSE")):
        if (root / source).read_bytes() != (root / bundled).read_bytes():
            errors.append(f"Bundled copy drift: {bundled} differs from {source}; rebuild the skill before committing")
    documents = {}
    texts = {}
    for relative in MARKDOWN_FILES:
        try:
            texts[relative] = (root / relative).read_text(encoding="utf-8")
            documents[relative] = _document(texts[relative])
            errors.extend(f"{relative}:{error}" for error in documents[relative].errors)
        except UnicodeError as exc:
            errors.append(f"{relative}: invalid UTF-8: {exc}")
    errors.extend(_skill_metadata(texts.get(f"{PACKAGE}/SKILL.md", "")))
    allowed = set(PUBLIC_FILES) | set(PUBLIC_SUPPORT_FILES)
    for relative, document in documents.items():
        for number, link in document.links:
            try:
                url = urllib.parse.urlsplit(link)
            except ValueError as exc:
                errors.append(f"{relative}:{number}: malformed link: {link} ({exc})")
                continue
            if url.scheme in ("http", "https", "mailto") or link.startswith("//"):
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(relative), urllib.parse.unquote(url.path))) if url.path else relative
            if url.scheme or target not in allowed:
                errors.append(f"{relative}:{number}: link targets a non-public path: {link}")
                continue
            destination = root / target
            if not destination.is_file() or destination.is_symlink():
                errors.append(f"{relative}:{number}: missing local link target: {link}")
                continue
            if url.fragment:
                linked = documents.get(target)
                if linked is None or urllib.parse.unquote(url.fragment) not in linked.anchors:
                    errors.append(f"{relative}:{number}: missing heading fragment: {link}")
    # Drift must fail before building; the builder deliberately refreshes its copies.
    if build and not errors:
        errors.extend(_build_checks(root))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true", help="only validate documentation and metadata")
    args = parser.parse_args()
    errors = check_repository(ROOT, build=not args.skip_build)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
    print("PASS: public links/anchors, JSON examples, skill metadata and bundled copies")
    if not args.skip_build:
        print("PASS: isolated three-file skill package and two identical rebuilds")


if __name__ == "__main__":
    main()
