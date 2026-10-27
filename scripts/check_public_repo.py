"""Fail closed on unexpected files and obvious local data before publishing."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    ".gitattributes", ".gitignore", "LICENSE", "README.md", "THIRD_PARTY_NOTICES.md",
    "pyproject.toml", "requirements.basic-memory.lock",
}
SOURCE_PREFIXES = ("src/aios/", "docs/", "templates/", "tests/", "scripts/")
ALLOWED_SUFFIXES = {".py", ".md", ".json", ".ps1", ".toml", ".lock", ".txt"}
FORBIDDEN_NAMES = (
    ".env", ".mcp.json", "AGENTS.md", "CLAUDE.md", "config.local",
    "credential", "secret", ".pem", ".key", ".p12", ".pfx",
    ".sqlite", ".db", ".log", ".enc", ".bundle",
)
CONTENT_RULES = {
    "home directory": re.compile(
        r"/" + r"Users/[^/\s]+/|" + r"/" + r"home/[^/\s]+/|"
        + r"[A-Za-z]:\\\\Users\\\\[^\\\\\s]+\\\\"
    ),
    "email address": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "private network address": re.compile(r"\b(?:192\.168|10\.\d{1,3})\.\d{1,3}\.\d{1,3}\b"),
}


def main() -> int:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True, check=True,
    )
    paths = sorted({Path(raw.decode("utf-8")) for raw in result.stdout.split(b"\0") if raw})
    issues: list[str] = []
    for relative in paths:
        name = relative.as_posix()
        path = ROOT / relative
        if not (name in ROOT_FILES or name.startswith(SOURCE_PREFIXES)):
            issues.append(f"{name}: unexpected publish path")
            continue
        if any(part.startswith(".") for part in relative.parts[1:]) or any(
            forbidden in name.lower() for forbidden in FORBIDDEN_NAMES
        ):
            issues.append(f"{name}: private or generated path")
            continue
        if (relative.suffix not in ALLOWED_SUFFIXES and name not in ROOT_FILES
                and name != "src/aios/evidence_search_ui.html"):
            issues.append(f"{name}: unexpected file type")
            continue
        if path.is_symlink() or not path.is_file():
            issues.append(f"{name}: missing or linked file")
            continue
        if path.stat().st_size > 2_000_000:
            issues.append(f"{name}: oversized file")
            continue
        try:
            contents = path.read_text(encoding="utf-8")
        except UnicodeError:
            issues.append(f"{name}: binary or invalid UTF-8")
            continue
        for label, pattern in CONTENT_RULES.items():
            if pattern.search(contents):
                issues.append(f"{name}: {label}")
    if issues:
        print("Public repository check failed:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1
    print(f"Public repository check passed for {len(paths)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
