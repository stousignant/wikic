#!/usr/bin/env python3
"""Install branch-independent publication gates in the shared Git directory."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OWNER_MARKER = "# wikic-publication-hook-v1"
SNAPSHOT_NAME = "leak_sweep.py"
HOOK_NAMES = ("pre-commit", "pre-push")


class InstallError(Exception):
    """An installation error with a fixed, publication-safe message."""


def _git_common_dir(root: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise InstallError("repository unavailable") from error
    path = Path(result.stdout.strip())
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _resolve_executable(value: str | None) -> Path:
    if value is None:
        discovered = shutil.which("gitleaks")
        if discovered is None:
            raise InstallError("credential scanner unavailable")
        path = Path(discovered)
    else:
        path = Path(value)
        if not path.is_absolute():
            raise InstallError("credential scanner path must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise InstallError("credential scanner unavailable") from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise InstallError("credential scanner unavailable")
    return resolved


def _python_executable() -> Path:
    try:
        executable = Path(sys.executable).resolve(strict=True)
    except OSError as error:
        raise InstallError("python runtime unavailable") from error
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise InstallError("python runtime unavailable")
    return executable


def _owned(path: Path) -> bool:
    try:
        return path.is_file() and OWNER_MARKER.encode() in path.read_bytes().splitlines()[:3]
    except OSError as error:
        raise InstallError("existing hook unreadable") from error


def _hook_text(name: str, python: Path, snapshot: Path, gitleaks: Path) -> str:
    checks = " || ".join(
        (
            f"[ ! -x {shlex.quote(str(python))} ]",
            f"[ ! -r {shlex.quote(str(snapshot))} ]",
            f"[ ! -x {shlex.quote(str(gitleaks))} ]",
        )
    )
    mode = "--staged" if name == "pre-commit" else "--pre-push"
    command = [
        str(python),
        str(snapshot),
        mode,
        "--credentials",
        "--gitleaks-executable",
        str(gitleaks),
    ]
    if name == "pre-push":
        rendered = " ".join(shlex.quote(part) for part in command)
        rendered += ' --remote-name "${1-}"'
    else:
        rendered = " ".join(shlex.quote(part) for part in command)
    return (
        "#!/bin/sh\n"
        f"{OWNER_MARKER}\n"
        f"if {checks}; then\n"
        "    echo 'publication-hook: required scanner unavailable' >&2\n"
        "    exit 2\n"
        "fi\n"
        f"exec {rendered}\n"
    )


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except OSError as error:
        raise InstallError("installation write failed") from error
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _backup_target(backups: Path, name: str) -> Path:
    candidate = backups / f"{name}.backup"
    suffix = 1
    while candidate.exists():
        candidate = backups / f"{name}.backup.{suffix}"
        suffix += 1
    return candidate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install shared publication hooks.")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    parser.add_argument("--gitleaks-executable")
    parser.add_argument("--replace-existing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def install(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    common = _git_common_dir(root)
    # A successful write to common/hooks is not installation when Git routes elsewhere.
    hook_setting = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"], cwd=root, capture_output=True
    )
    if hook_setting.returncode == 0:
        raise InstallError("custom hooks path requires explicit integration")
    if hook_setting.returncode != 1:
        raise InstallError("hook configuration unavailable")
    gitleaks = _resolve_executable(args.gitleaks_executable)
    python = _python_executable()
    source = Path(__file__).with_name(SNAPSHOT_NAME)
    try:
        snapshot_data = source.read_bytes()
    except OSError as error:
        raise InstallError("reviewed scanner unavailable") from error

    publication = common / "publication"
    snapshot = publication / SNAPSHOT_NAME
    hooks = common / "hooks"
    hook_paths = {name: hooks / name for name in HOOK_NAMES}
    if any(path.is_symlink() for path in hook_paths.values()):
        raise InstallError("symlink hooks require manual integration")
    foreign = [path for path in hook_paths.values() if os.path.lexists(path) and not _owned(path)]
    if foreign and not args.replace_existing:
        raise InstallError("existing hook not owned; use --replace-existing")

    hook_data = {name: _hook_text(name, python, snapshot, gitleaks).encode() for name in HOOK_NAMES}
    if args.dry_run:
        print("publication-hooks: dry-run complete")
        return

    try:
        publication.mkdir(parents=True, exist_ok=True, mode=0o700)
        publication.chmod(0o700)
        hooks.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise InstallError("installation directory unavailable") from error

    if foreign:
        backups = publication / "backups"
        try:
            backups.mkdir(parents=True, exist_ok=True, mode=0o700)
            backups.chmod(0o700)
        except OSError as error:
            raise InstallError("backup directory unavailable") from error
        for path in foreign:
            try:
                original = path.read_bytes()
            except OSError as error:
                raise InstallError("existing hook unreadable") from error
            _atomic_write(_backup_target(backups, path.name), original, 0o600)

    _atomic_write(snapshot, snapshot_data, 0o600)
    for name, path in hook_paths.items():
        _atomic_write(path, hook_data[name], 0o700)
    print("publication-hooks: installed")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        install(args)
    except InstallError as error:
        print(f"publication-hooks: error={error}", file=sys.stderr)
        return 2
    except Exception:
        print("publication-hooks: error=unexpected installation failure", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
