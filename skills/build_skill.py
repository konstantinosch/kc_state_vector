"""Build the portable skill from its instructions and the canonical pattern.

Run with Python 3 from any working directory. Only explicitly listed files enter
the archive; local research and benchmark files are never collected. Instructions
remain readable without Python; the optional self-check kit uses its standard library.
"""

from pathlib import Path
import hashlib
import re
import zipfile


ARCHIVE_MEMBERS = (
    "SKILL.md", "references/kc-state-vector.md", "LICENSE",
    "references/self-check.md", "references/self-check-examples.md",
    "scripts/oracle.py", "scripts/check_pager.py", "scripts/sample_pager.py",
    "scripts/template_pager.py", "scripts/mutations.py",
    "scripts/fixtures/priority_51_49_48.json", "scripts/fixtures/full_tuple_ties.json",
    "scripts/fixtures/decimal_id_ties.json", "scripts/fixtures/empty_duplicate_page.json",
    "scripts/fixtures/three_overlapping_stages.json", "scripts/fixtures/unread_batch_tail.json",
    "scripts/fixtures/empty_plan.json", "scripts/fixtures/empty_sources.json",
    "scripts/fixtures/zero_quotas.json", "scripts/fixtures/all_zero_quotas.json",
)


def main():
    root = Path(__file__).resolve().parent.parent
    package = root / "skills" / "kc-state-vector"
    instructions = package / "SKILL.md"
    if not instructions.is_file():
        raise SystemExit(f"Missing skill instructions: {instructions}")

    text = instructions.read_text(encoding="utf-8")
    if not re.match(r"\A---\nname: kc-state-vector\n", text):
        raise SystemExit("The skill name must match the package directory.")

    reference = package / "references" / "kc-state-vector.md"
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_bytes((root / "kc-state-vector.md").read_bytes())
    license_file = package / "LICENSE"
    license_file.write_bytes((root / "LICENSE").read_bytes())

    files = [package / name for name in ARCHIVE_MEMBERS]
    for path in files:
        if (not path.is_file() or path.is_symlink()
                or not path.resolve().is_relative_to(package.resolve())):
            raise SystemExit(f"Missing or unsupported package input: {path}")
    output = root / "dist" / "kc-state-vector.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            name = path.relative_to(package.parent).as_posix()
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes())

    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise SystemExit("Archive integrity check failed.")
        expected = [path.relative_to(package.parent).as_posix() for path in files]
        if archive.namelist() != expected:
            raise SystemExit("Archive contents differ from the explicit file list.")
        for path, name in zip(files, expected):
            if archive.read(name) != path.read_bytes():
                raise SystemExit(f"Archive content mismatch: {name}")

    print(f"Built {output} ({output.stat().st_size:,} bytes; {len(files)} files)")
    print(f"SHA-256: {hashlib.sha256(output.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
