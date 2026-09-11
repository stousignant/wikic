#!/usr/bin/env python3
"""Scan publication inputs for identity/path patterns and credentials.

Private regular expressions live outside every worktree in
``$(git rev-parse --git-common-dir)/info/leakpatterns``. Set the repository-local
``leakSweep.requirePatterns`` boolean to make that file mandatory.

The scanner reports only finding category, fixed pattern type, and count.
It never prints matched text, file names, refs, patterns, or subprocess errors.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

BUILTIN_PATTERNS: dict[str, str] = {
    "absolute-home-path": r"/(?:home|Users)/[A-Za-z0-9._-]+/",
    "windows-user-path": r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+",
}
ZERO_RE = re.compile(r"^0+$")
OID_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


class ScanError(Exception):
    """An error safe to summarize without exposing its underlying data."""


@dataclass(frozen=True)
class Finding:
    category: str
    pattern: str


def _run_git(root: Path, args: list[str], *, text: bool = False) -> bytes | str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, check=True, text=text
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ScanError("git command failed") from error
    return result.stdout


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ScanError("non-UTF-8 publication input rejected") from error


def git_common_dir(root: Path) -> Path:
    value = str(_run_git(root, ["rev-parse", "--git-common-dir"], text=True)).strip()
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def require_private_patterns(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "config", "--local", "--bool", "--get", "leakSweep.requirePatterns"],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise ScanError("git config lookup failed") from error
    if result.returncode == 1:
        return False
    if result.returncode != 0 or result.stdout.strip() not in {"true", "false"}:
        raise ScanError("invalid private-pattern requirement setting")
    return result.stdout.strip() == "true"


def _read_pattern_lines(path: Path, *, required: bool) -> list[str]:
    if not path.is_file():
        if required:
            raise ScanError("required private patterns unavailable")
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ScanError("private patterns unreadable") from error
    entries = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if required and not entries:
        raise ScanError("required private patterns empty")
    return entries


def load_patterns(root: Path, patterns_file: Path | None = None) -> dict[str, re.Pattern[str]]:
    required = require_private_patterns(root)
    source = patterns_file or (git_common_dir(root) / "info" / "leakpatterns")
    file_entries = _read_pattern_lines(source, required=required)
    env_entries = [
        line.strip()
        for line in os.environ.get("LEAK_PATTERNS", "").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    raw = [*BUILTIN_PATTERNS.items()]
    raw.extend((f"custom-{index + 1}", value) for index, value in enumerate(file_entries))
    offset = len(file_entries)
    raw.extend((f"custom-{offset + index + 1}", value) for index, value in enumerate(env_entries))
    try:
        return {name: re.compile(value) for name, value in raw}
    except re.error as error:
        raise ScanError("invalid privacy pattern") from error


def scan_text(text: str, compiled: dict[str, re.Pattern[str]]) -> list[Finding]:
    findings: list[Finding] = []
    for name, pattern in compiled.items():
        findings.extend(Finding("privacy", name) for _match in pattern.finditer(text))
    return findings


def _cat_file(root: Path, oid: str) -> tuple[str, bytes]:
    kind = str(_run_git(root, ["cat-file", "-t", oid], text=True)).strip()
    data = _run_git(root, ["cat-file", kind, oid])
    assert isinstance(data, bytes)
    return kind, data


def _staged_chunks(root: Path, excludes: set[str]) -> list[bytes]:
    output = _run_git(root, ["ls-files", "--stage", "-z"])
    assert isinstance(output, bytes)
    chunks: list[bytes] = []
    seen: set[str] = set()
    for record in output.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            _mode, raw_oid, raw_stage = metadata.split()
        except ValueError as error:
            raise ScanError("invalid staged index record") from error
        path = _decode(raw_path)
        if path in excludes:
            continue
        if raw_stage != b"0":
            raise ScanError("unmerged staged entry rejected")
        oid = raw_oid.decode("ascii")
        chunks.append(raw_path)
        if oid not in seen:
            kind, data = _cat_file(root, oid)
            if kind == "blob":
                chunks.append(data)
            seen.add(oid)
    return chunks


def _tree_chunks(root: Path, oid: str, seen_blobs: set[str]) -> list[bytes]:
    chunks: list[bytes] = []
    tree = _run_git(root, ["ls-tree", "-r", "-z", "--full-tree", oid])
    assert isinstance(tree, bytes)
    for record in tree.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            _mode, raw_kind, raw_blob_oid = metadata.split()
            blob_oid = raw_blob_oid.decode("ascii")
        except (UnicodeDecodeError, ValueError) as error:
            raise ScanError("invalid tree record") from error
        chunks.append(raw_path)
        if raw_kind == b"blob" and blob_oid not in seen_blobs:
            blob_kind, blob_data = _cat_file(root, blob_oid)
            if blob_kind != "blob":
                raise ScanError("unexpected tree object")
            chunks.append(blob_data)
            seen_blobs.add(blob_oid)
    return chunks


def _revision_chunks(
    root: Path, revision_args: list[str], seen_blobs: set[str] | None = None
) -> list[bytes]:
    chunks: list[bytes] = []
    commits = _run_git(root, ["rev-list", *revision_args])
    assert isinstance(commits, bytes)
    if seen_blobs is None:
        seen_blobs = set()

    for raw_oid in commits.splitlines():
        if raw_oid:
            commit_oid = raw_oid.decode("ascii")
            kind, data = _cat_file(root, commit_oid)
            if kind != "commit":
                raise ScanError("unexpected revision object")
            chunks.append(data)
            chunks.extend(_tree_chunks(root, commit_oid, seen_blobs))
    return chunks


def _tag_chunks(root: Path, oid: str) -> list[bytes]:
    chunks: list[bytes] = []
    seen: set[str] = set()
    while oid not in seen:
        seen.add(oid)
        kind, data = _cat_file(root, oid)
        if kind == "blob":
            chunks.append(data)
            break
        if kind == "tree":
            chunks.extend(_tree_chunks(root, oid, set()))
            break
        if kind != "tag":
            break
        chunks.append(data)
        first = data.splitlines()[0] if data else b""
        if not first.startswith(b"object "):
            raise ScanError("invalid annotated tag object")
        oid = first.removeprefix(b"object ").decode("ascii")
    return chunks


def _history_chunks(root: Path) -> list[bytes]:
    chunks = _revision_chunks(root, ["--all"])
    refs = _run_git(root, ["for-each-ref", "--format=%(refname)%00%(objectname)", "refs"])
    assert isinstance(refs, bytes)
    for record in refs.splitlines():
        raw_ref, separator, raw_oid = record.partition(b"\0")
        if not separator:
            raise ScanError("invalid ref record")
        chunks.append(raw_ref)
        chunks.extend(_tag_chunks(root, raw_oid.decode("ascii")))
    return chunks


def _parse_updates(data: bytes) -> list[tuple[bytes, str, bytes, str]]:
    updates: list[tuple[bytes, str, bytes, str]] = []
    for line in data.splitlines():
        parts = line.split()
        if len(parts) != 4:
            raise ScanError("invalid pre-push update record")
        local_ref, raw_local_oid, remote_ref, raw_remote_oid = parts
        try:
            local_oid = raw_local_oid.decode("ascii")
            remote_oid = raw_remote_oid.decode("ascii")
        except UnicodeDecodeError as error:
            raise ScanError("invalid pre-push object id") from error
        if not OID_RE.fullmatch(local_oid) or not OID_RE.fullmatch(remote_oid):
            raise ScanError("invalid pre-push object id")
        updates.append((local_ref, local_oid, remote_ref, remote_oid))
    if not updates:
        raise ScanError("pre-push update data unavailable")
    return updates


def _pre_push_chunks(root: Path, data: bytes, remote_name: str | None) -> list[bytes]:
    chunks: list[bytes] = []
    seen_blobs: set[str] = set()
    for local_ref, local_oid, remote_ref, remote_oid in _parse_updates(data):
        if ZERO_RE.fullmatch(local_oid):
            continue
        chunks.extend((local_ref, remote_ref))
        if ZERO_RE.fullmatch(remote_oid):
            revisions = [local_oid]
            if remote_name:
                revisions.extend(["--not", f"--remotes={remote_name}"])
        else:
            revisions = [f"{remote_oid}..{local_oid}"]
        chunks.extend(_revision_chunks(root, revisions, seen_blobs))
        chunks.extend(_tag_chunks(root, local_oid))
    return chunks


def scan_credentials(chunks: Iterable[bytes], executable: str = "gitleaks") -> list[Finding]:
    with tempfile.TemporaryDirectory(prefix="publication-scan-") as directory:
        report = Path(directory) / "report.json"
        with tempfile.TemporaryFile() as stream:
            for chunk in chunks:
                stream.write(chunk)
                stream.write(b"\n-- publication boundary --\n")
            stream.seek(0)
            try:
                result = subprocess.run(
                    [
                        executable,
                        "stdin",
                        "--no-banner",
                        "--no-color",
                        "--log-level",
                        "error",
                        "--redact",
                        "--report-format",
                        "json",
                        "--report-path",
                        str(report),
                    ],
                    stdin=stream,
                    capture_output=True,
                    timeout=300,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise ScanError("credential scanner unavailable") from error
        if result.returncode not in {0, 1}:
            raise ScanError("credential scanner failed")
        try:
            payload = json.loads(report.read_text(encoding="utf-8")) if report.exists() else []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ScanError("credential scanner report invalid") from error
        if not isinstance(payload, list):
            raise ScanError("credential scanner report invalid")
        findings: list[Finding] = []
        for item in payload:
            if not isinstance(item, dict):
                raise ScanError("credential scanner report invalid")
            # Detector fields are untrusted and may themselves contain private values.
            findings.append(Finding("credential", "generic"))
        if result.returncode == 1 and not findings:
            raise ScanError("credential scanner failed without report")
        if result.returncode == 0 and findings:
            raise ScanError("credential scanner status inconsistent")
        return findings


def _read_updates(args: argparse.Namespace) -> bytes:
    path_value = args.updates_file or os.environ.get("LEAK_SWEEP_UPDATES_FILE")
    if path_value:
        try:
            return Path(path_value).read_bytes()
        except OSError as error:
            raise ScanError("pre-push update data unreadable") from error
    if sys.stdin.isatty():
        raise ScanError("pre-push update data unavailable")
    return sys.stdin.buffer.read()


def _render(findings: list[Finding], custom_count: int) -> int:
    print(f"leak-sweep: patterns={len(BUILTIN_PATTERNS) + custom_count} custom={custom_count}")
    if not findings:
        print("leak-sweep: clean")
        return 0
    counts = Counter((finding.category, finding.pattern) for finding in findings)
    for (category, pattern), count in sorted(counts.items()):
        print(f"leak-sweep: finding category={category} type={pattern} count={count}")
    print(f"leak-sweep: blocked findings={len(findings)}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely scan publication inputs.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--staged", action="store_true", help="Scan paths and blobs from the index.")
    modes.add_argument(
        "--history", action="store_true", help="Scan all reachable history and refs."
    )
    modes.add_argument("--messages", action="store_true", help=argparse.SUPPRESS)
    modes.add_argument("--pre-push", action="store_true", help="Scan exact pre-push update ranges.")
    modes.add_argument(
        "--text-file", type=Path, help="Scan local publication prose; implies credentials."
    )
    modes.add_argument("--text-file-env", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--patterns-file", type=Path)
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument(
        "--credentials", action="store_true", help="Also run gitleaks over scan input."
    )
    parser.add_argument("--gitleaks-executable", default="gitleaks", help=argparse.SUPPRESS)
    parser.add_argument("--updates-file", help=argparse.SUPPRESS)
    parser.add_argument("--remote-name", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        compiled = load_patterns(root, args.patterns_file)
        text_file = args.text_file
        if args.text_file_env:
            text_file_value = os.environ.get("LEAK_SWEEP_TEXT_FILE")
            if not text_file_value:
                raise ScanError("publication text input unavailable")
            text_file = Path(text_file_value)
        if text_file:
            try:
                chunks = [text_file.read_bytes()]
            except OSError as error:
                raise ScanError("publication text input unreadable") from error
        elif args.history or args.messages:
            chunks = _history_chunks(root)
        elif args.pre_push:
            remote = args.remote_name or os.environ.get("LEAK_SWEEP_REMOTE_NAME")
            chunks = _pre_push_chunks(root, _read_updates(args), remote)
        else:
            chunks = _staged_chunks(root, set(args.exclude))

        findings: list[Finding] = []
        for chunk in chunks:
            findings.extend(scan_text(_decode(chunk), compiled))
        if args.credentials or text_file:
            findings.extend(scan_credentials(chunks, args.gitleaks_executable))
        custom_count = sum(name.startswith("custom-") for name in compiled)
        return _render(findings, custom_count)
    except ScanError as error:
        # ScanError messages are a fixed vocabulary and contain no source data.
        print(f"leak-sweep: error={error}", file=sys.stderr)
        return 2
    except Exception:
        # Fail closed without allowing unexpected exception data to disclose inputs.
        print("leak-sweep: error=unexpected scan failure", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
