import os
import stat
import subprocess
import sys
from pathlib import Path

INSTALLER = Path(__file__).resolve().parent.parent / "scripts" / "install_publication_hooks.py"
SCANNER = Path(__file__).resolve().parent.parent / "scripts" / "leak_sweep.py"
CONFIG = Path(__file__).resolve().parent.parent / ".pre-commit-config.yaml"


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test Author")
    git(path, "config", "user.email", "test@example.test")
    return path


def commit_all(root: Path, message: str = "fixture") -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD").stdout.decode().strip()


def fake_gitleaks(tmp_path: Path) -> Path:
    executable = tmp_path / "gitleaks"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

data = sys.stdin.buffer.read()
log = os.environ.get("HOOK_LOG")
if log:
    with pathlib.Path(log).open("ab") as stream:
        stream.write(b"called\\n")
needle = os.environ.get("FAKE_SECRET", "not-present").encode()
count = data.count(needle)
report = pathlib.Path(sys.argv[sys.argv.index("--report-path") + 1])
report.write_text(json.dumps([{"Secret": "hidden"}] * count), encoding="utf-8")
raise SystemExit(1 if count else 0)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def run_installer(
    root: Path,
    *args: str,
    gitleaks: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    command = [sys.executable, str(INSTALLER)]
    if gitleaks is not None:
        command.extend(["--gitleaks-executable", str(gitleaks)])
    command.extend(args)
    environment = os.environ.copy()
    environment.pop("LEAK_PATTERNS", None)
    if env:
        environment.update(env)
    return subprocess.run(
        command,
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
    )


def common_dir(root: Path) -> Path:
    value = Path(git(root, "rev-parse", "--git-common-dir").stdout.decode().strip())
    return value if value.is_absolute() else (root / value).resolve()


def test_snapshot_hooks_work_from_two_worktrees_without_current_config(tmp_path: Path) -> None:
    primary = init_repo(tmp_path / "primary")
    (primary / "base.txt").write_text("base\n", encoding="utf-8")
    commit_all(primary)
    linked = tmp_path / "linked"
    git(primary, "worktree", "add", "-q", "-b", "old-config", str(linked))
    (linked / ".pre-commit-config.yaml").write_text("invalid: old config\n", encoding="utf-8")
    commit_all(linked, "old config")
    assert not (primary / ".pre-commit-config.yaml").exists()
    tool = fake_gitleaks(tmp_path)
    log = tmp_path / "hook-log"
    environment = {**os.environ, "HOOK_LOG": str(log)}

    installed = run_installer(primary, gitleaks=tool)
    hook = common_dir(primary) / "hooks" / "pre-commit"
    (primary / "primary.txt").write_text("safe\n", encoding="utf-8")
    git(primary, "add", "primary.txt")
    primary_run = subprocess.run([str(hook)], cwd=primary, env=environment, capture_output=True)
    (linked / "linked.txt").write_text("safe\n", encoding="utf-8")
    git(linked, "add", "linked.txt")
    linked_run = subprocess.run([str(hook)], cwd=linked, env=environment, capture_output=True)

    publication = common_dir(primary) / "publication"
    snapshot = publication / "leak_sweep.py"
    assert installed.returncode == 0
    assert primary_run.returncode == 0
    assert linked_run.returncode == 0
    assert log.read_bytes().splitlines() == [b"called", b"called"]
    assert snapshot.read_bytes() == SCANNER.read_bytes()
    assert stat.S_IMODE(publication.stat().st_mode) == 0o700
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    hook_text = hook.read_text(encoding="utf-8")
    assert ".pre-commit-config.yaml" not in hook_text
    assert "pre-commit run" not in hook_text


def test_pre_push_hook_passes_raw_multi_ref_stdin_to_snapshot(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    base = commit_all(root)
    git(root, "checkout", "-q", "-b", "clean")
    clean = commit_all(root, "clean update")
    marker = "second" + "-raw-update-marker"
    git(root, "checkout", "-q", "-b", "finding", base)
    finding = commit_all(root, marker)
    tool = fake_gitleaks(tmp_path)
    installed = run_installer(root, gitleaks=tool)
    hook = common_dir(root) / "hooks" / "pre-push"
    updates = (
        f"refs/heads/clean {clean} refs/heads/clean {base}\n"
        f"refs/heads/finding {finding} refs/heads/finding {base}\n"
    ).encode()

    pushed = subprocess.run(
        [str(hook), "origin", "example.invalid/repo"],
        cwd=root,
        env={**os.environ, "FAKE_SECRET": marker},
        input=updates,
        capture_output=True,
    )

    assert installed.returncode == 0
    assert pushed.returncode == 1
    assert b"category=credential type=generic count=1" in pushed.stdout
    assert marker.encode() not in pushed.stdout + pushed.stderr
    hook_text = hook.read_text(encoding="utf-8")
    assert "LEAK_SWEEP_UPDATES_FILE" not in hook_text
    assert "exec " in hook_text


def test_installer_fails_closed_when_gitleaks_is_missing_or_not_absolute(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()

    missing_default = run_installer(root, env={"PATH": str(empty_path)})
    relative = run_installer(root, "--gitleaks-executable", "gitleaks")
    missing_absolute = run_installer(
        root, "--gitleaks-executable", str(tmp_path / "missing-gitleaks")
    )

    for result in (missing_default, relative, missing_absolute):
        assert result.returncode == 2
        assert result.stderr.startswith("publication-hooks: error=")
    assert not (common_dir(root) / "publication").exists()
    assert not (common_dir(root) / "hooks" / "pre-commit").exists()


def test_installer_resolves_default_gitleaks_from_path(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    tool = fake_gitleaks(tmp_path)
    environment = {"PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}

    result = run_installer(root, env=environment)

    assert result.returncode == 0
    hook = (common_dir(root) / "hooks" / "pre-commit").read_text(encoding="utf-8")
    assert str(tool.resolve()) in hook


def test_installed_hook_sanitizes_scanner_bootstrap_failure(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    tool = fake_gitleaks(tmp_path)
    assert run_installer(root, gitleaks=tool).returncode == 0
    hook = common_dir(root) / "hooks" / "pre-commit"
    tool.unlink()

    result = subprocess.run([str(hook)], cwd=root, capture_output=True, text=True)

    assert result.returncode == 2
    assert result.stderr.strip() == "publication-hook: required scanner unavailable"
    assert str(tmp_path) not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_existing_unowned_hook_is_refused_preserved_and_backed_up_on_replace(
    tmp_path: Path,
) -> None:
    root = init_repo(tmp_path / "repo")
    tool = fake_gitleaks(tmp_path)
    hook = common_dir(root) / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    unsafe_marker = "unsafe" + "-old-hook-output"
    original = f"#!/bin/sh\necho {unsafe_marker}\n".encode()
    hook.write_bytes(original)
    hook.chmod(0o755)

    refused = run_installer(root, gitleaks=tool)

    assert refused.returncode == 2
    assert hook.read_bytes() == original
    assert not (common_dir(root) / "publication").exists()

    replaced = run_installer(root, "--replace-existing", gitleaks=tool)
    invocation = subprocess.run([str(hook)], cwd=root, capture_output=True, text=True)
    backups = list((common_dir(root) / "publication" / "backups").glob("pre-commit.backup*"))

    assert replaced.returncode == 0
    assert invocation.returncode == 0
    assert unsafe_marker not in invocation.stdout + invocation.stderr
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(backups[0].parent.stat().st_mode) == 0o700


def test_dry_run_with_replacement_makes_no_mutations(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    tool = fake_gitleaks(tmp_path)
    hook = common_dir(root) / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    original = b"#!/bin/sh\nexit 7\n"
    hook.write_bytes(original)

    result = run_installer(root, "--replace-existing", "--dry-run", gitleaks=tool)

    assert result.returncode == 0
    assert result.stdout.strip() == "publication-hooks: dry-run complete"
    assert hook.read_bytes() == original
    assert not (common_dir(root) / "publication").exists()
    assert not (common_dir(root) / "hooks" / "pre-commit").exists()


def test_custom_hooks_path_is_not_silently_ignored(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    tool = fake_gitleaks(tmp_path)
    git(root, "config", "core.hooksPath", str(tmp_path / "custom-hooks"))
    result = run_installer(root, gitleaks=tool)
    assert result.returncode == 2
    assert "custom hooks path" in result.stderr
    assert str(tmp_path) not in result.stderr
    assert not (common_dir(root) / "publication").exists()


def test_symlink_hooks_are_preserved_even_with_replacement_requested(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    tool = fake_gitleaks(tmp_path)
    target = tmp_path / "hook-target"
    hook = common_dir(root) / "hooks" / "pre-commit"
    hook.symlink_to(target)
    for target_exists in (False, True):
        if target_exists:
            target.write_text("#!/bin/sh\nexit 7\n")
        for flags in ((), ("--replace-existing",)):
            result = run_installer(root, *flags, gitleaks=tool)
            assert result.returncode == 2
            assert hook.is_symlink()
            assert hook.readlink() == target
            assert target.exists() == target_exists
            assert not (common_dir(root) / "publication").exists()


def test_owned_reinstall_is_idempotent_and_creates_no_backup(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    tool = fake_gitleaks(tmp_path)

    first = run_installer(root, gitleaks=tool)
    common = common_dir(root)
    before = {name: (common / "hooks" / name).read_bytes() for name in ("pre-commit", "pre-push")}
    second = run_installer(root, gitleaks=tool)
    after = {name: (common / "hooks" / name).read_bytes() for name in before}

    assert first.returncode == 0
    assert second.returncode == 0
    assert before == after
    assert not (common / "publication" / "backups").exists()


def test_pre_commit_hook_invokes_scanner_for_deletion_only_and_empty_index(
    tmp_path: Path,
) -> None:
    root = init_repo(tmp_path / "repo")
    tracked = root / "tracked.txt"
    tracked.write_text("safe\n", encoding="utf-8")
    commit_all(root)
    tool = fake_gitleaks(tmp_path)
    log = tmp_path / "hook-log"
    assert run_installer(root, gitleaks=tool).returncode == 0
    hook = common_dir(root) / "hooks" / "pre-commit"
    environment = {**os.environ, "HOOK_LOG": str(log)}

    tracked.unlink()
    git(root, "add", "-u")
    deletion_only = subprocess.run([str(hook)], cwd=root, env=environment, capture_output=True)
    git(root, "reset", "--hard", "-q", "HEAD")
    git(root, "rm", "--cached", "-q", "tracked.txt")
    empty_index = subprocess.run([str(hook)], cwd=root, env=environment, capture_output=True)

    assert deletion_only.returncode == 0
    assert empty_index.returncode == 0
    assert log.read_bytes().splitlines() == [b"called", b"called"]


def test_pre_commit_config_marks_every_security_hook_always_run() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    assert config.count("always_run: true") == 4
    assert "default_install_hook_types" not in config
    assert "publication-sweep-staged-credentials" in config
    assert "stages: [pre-commit]" in config
    assert "publication-sweep-push" in config
    assert "stages: [pre-push]" in config
    assert "publication-sweep-text" in config
    assert "stages: [manual]" in config
    assert "gitleaks git --redact --log-opts=--all" not in config
