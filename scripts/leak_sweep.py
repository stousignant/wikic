#!/usr/bin/env python3
"""Deterministic identity and path leak sweep.

Secret scanners look for credentials. For a personal toolchain the dominant leak
class is different: absolute home paths, machine names, employer names, and
internal tool names that reveal who built the thing and where.

Structural patterns are built in because they are safe to publish. Anything
environment-specific stays out of the repository and is supplied at runtime via
``--patterns-file`` (default ``.leakpatterns``, gitignored) or the
``LEAK_PATTERNS`` environment variable, one regex per line. Committing the
private pattern list would itself be the leak this script exists to prevent.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BUILTIN_PATTERNS: dict[str, str] = {
    "absolute-home-path": r"/(?:home|Users)/[A-Za-z0-9._-]+/",
    "windows-user-path": r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+",
}

# Binary and vendored paths that would produce noise without adding signal.
SKIP_SUFFIXES = (".lock", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".woff", ".woff2")


@dataclass(frozen=True)
class Finding:
    location: str
    line: int
    pattern: str
    excerpt: str

    def render(self) -> str:
        return f"{self.location}:{self.line}: [{self.pattern}] {self.excerpt}"


def redact(text: str, match: re.Match[str]) -> str:
    """Show the surrounding line with the sensitive span masked."""
    start, end = match.span()
    line = text[:start] + "<REDACTED>" + text[end:]
    return line.strip()[:160]


def load_patterns(patterns_file: Path | None) -> dict[str, str]:
    patterns = dict(BUILTIN_PATTERNS)
    raw: list[str] = []

    env_value = os.environ.get("LEAK_PATTERNS", "")
    if env_value.strip():
        raw.extend(env_value.splitlines())

    if patterns_file and patterns_file.is_file():
        raw.extend(patterns_file.read_text(encoding="utf-8").splitlines())

    for index, entry in enumerate(raw):
        candidate = entry.strip()
        if not candidate or candidate.startswith("#"):
            continue
        patterns[f"custom-{index}"] = candidate

    return patterns


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [root / name for name in result.stdout.split("\0") if name]


def scan_text(location: str, text: str, compiled: dict[str, re.Pattern[str]]) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in compiled.items():
            match = pattern.search(line)
            if match:
                findings.append(Finding(location, number, name, redact(line, match)))
    return findings


def scan_files(root: Path, compiled: dict[str, re.Pattern[str]], skip: set[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in tracked_files(root):
        if path in skip or path.suffix in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(scan_text(str(path.relative_to(root)), text, compiled))
    return findings


def scan_messages(root: Path, compiled: dict[str, re.Pattern[str]]) -> list[Finding]:
    """Commit messages are not blobs, so blob-oriented scanners never see them."""
    result = subprocess.run(
        ["git", "log", "--all", "--format=%H%n%s%n%b%n--"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return scan_text("<commit-messages>", result.stdout, compiled)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan for identity and path leaks.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--patterns-file", type=Path, default=Path(".leakpatterns"))
    parser.add_argument("--messages", action="store_true", help="Also scan commit messages.")
    parser.add_argument(
        "--exclude", action="append", default=[], help="Repo-relative path to skip."
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    patterns = load_patterns(args.patterns_file)
    compiled = {name: re.compile(pattern) for name, pattern in patterns.items()}
    skip = {(root / name).resolve() for name in args.exclude}

    findings = scan_files(root, compiled, skip)
    if args.messages:
        findings.extend(scan_messages(root, compiled))

    custom = sum(1 for name in patterns if name.startswith("custom-"))
    print(f"leak-sweep: {len(patterns)} patterns ({custom} supplied at runtime)")

    if not findings:
        print("leak-sweep: clean")
        return 0

    for finding in findings:
        print(finding.render())
    print(f"leak-sweep: {len(findings)} finding(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
