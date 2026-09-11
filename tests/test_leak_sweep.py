import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "leak_sweep.py"
SPEC = importlib.util.spec_from_file_location("leak_sweep", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
leak_sweep = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = leak_sweep
SPEC.loader.exec_module(leak_sweep)


def git(root: Path, *args: str, input_data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, input=input_data, capture_output=True, check=True
    )


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


def run_scan(
    root: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    clean_env = os.environ.copy()
    clean_env.pop("LEAK_PATTERNS", None)
    if env:
        clean_env.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        cwd=root,
        capture_output=True,
        text=True,
        env=clean_env,
    )


def write_private_patterns(root: Path, pattern: str) -> Path:
    common = Path(git(root, "rev-parse", "--git-common-dir").stdout.decode().strip())
    if not common.is_absolute():
        common = root / common
    target = common / "info" / "leakpatterns"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(pattern + "\n", encoding="utf-8")
    return target


def fake_gitleaks(tmp_path: Path) -> Path:
    executable = tmp_path / "gitleaks-fixture"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

data = sys.stdin.buffer.read()
needle = os.environ["FAKE_SECRET"].encode()
report = pathlib.Path(sys.argv[sys.argv.index("--report-path") + 1])
synthetic_report = os.environ.get("FAKE_REPORT_JSON")
if synthetic_report is not None:
    report.write_text(synthetic_report, encoding="utf-8")
    raise SystemExit(1)
items = []
start = 0
while True:
    index = data.find(needle, start)
    if index < 0:
        break
    items.append({"RuleID": "fixture-rule", "Secret": needle.decode()})
    start = index + len(needle)
report.write_text(json.dumps(items), encoding="utf-8")
raise SystemExit(1 if items else 0)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def test_builtin_patterns_match_common_home_layouts() -> None:
    unix_path = "/" + "home/sample/notes.md"
    mac_path = "/" + "Users/sample/Documents/notes.md"
    windows_path = "C:" + r"\Users\sample\Documents\notes.md"
    assert re.search(leak_sweep.BUILTIN_PATTERNS["absolute-home-path"], unix_path)
    assert re.search(leak_sweep.BUILTIN_PATTERNS["absolute-home-path"], mac_path)
    assert re.search(leak_sweep.BUILTIN_PATTERNS["windows-user-path"], windows_path)
    assert not re.search(leak_sweep.BUILTIN_PATTERNS["absolute-home-path"], "https://example.test")


def test_linked_worktree_uses_shared_common_dir_patterns(tmp_path: Path) -> None:
    primary = init_repo(tmp_path / "primary")
    (primary / "base.txt").write_text("base\n", encoding="utf-8")
    commit_all(primary)
    marker = "Private" + "Marker"
    write_private_patterns(primary, marker)
    linked = tmp_path / "linked"
    git(primary, "worktree", "add", "-q", "-b", "linked-test", str(linked))
    (linked / "new.txt").write_text(marker, encoding="utf-8")
    git(linked, "add", "new.txt")

    result = run_scan(linked, "--staged")

    assert result.returncode == 1
    assert "type=custom-1" in result.stdout
    assert marker not in result.stdout + result.stderr


def test_required_patterns_fail_closed_when_shared_file_missing_or_empty(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    git(root, "config", "leakSweep.requirePatterns", "true")

    missing = run_scan(root, "--staged")
    path = write_private_patterns(root, "# no active entries")
    empty = run_scan(root, "--staged")
    path.write_text("[unterminated\n", encoding="utf-8")
    invalid = run_scan(root, "--staged")

    for result in (missing, empty, invalid):
        assert result.returncode == 2
        assert "leak-sweep: error=" in result.stderr
        assert str(path) not in result.stderr
    assert "unterminated" not in invalid.stderr


def test_approved_identity_is_limited_to_true_git_headers(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    marker = "Public" + "AuthorMarker"
    identity = f"{marker} <author@example.test>"
    write_private_patterns(root, marker)
    git(root, "config", "user.name", marker)
    git(root, "config", "user.email", "author@example.test")
    git(root, "config", "leakSweep.allowedIdentity", identity)
    commit_all(root, "safe message")
    git(root, "tag", "-a", "safe-tag", "-m", "safe annotation")
    assert run_scan(root, "--history").returncode == 0
    imitation = f"author {identity} 1234567890 +0000\n\nsafe body"
    (root / "note.txt").write_text(imitation)
    git(root, "add", "note.txt")
    assert run_scan(root, "--staged").returncode == 1
    commit_all(root, imitation)
    assert run_scan(root, "--history").returncode == 1
    prose = tmp_path / "prose"
    prose.write_text(imitation)
    executable = fake_gitleaks(tmp_path)
    assert (
        run_scan(
            root,
            "--text-file",
            str(prose),
            "--gitleaks-executable",
            str(executable),
            env={"FAKE_SECRET": "unmatched-credential-fixture"},
        ).returncode
        == 1
    )


def test_identity_exception_never_bypasses_credential_scanner(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    marker = "Allowed" + "CredentialFixture"
    git(root, "config", "user.name", marker)
    git(root, "config", "leakSweep.allowedIdentity", f"{marker} <test@example.test>")
    write_private_patterns(root, marker)
    commit_all(root)
    executable = fake_gitleaks(tmp_path)
    result = run_scan(
        root,
        "--history",
        "--credentials",
        "--gitleaks-executable",
        str(executable),
        env={"FAKE_SECRET": marker},
    )
    assert result.returncode == 1
    assert "category=credential" in result.stdout
    assert "category=privacy" not in result.stdout
    assert marker not in result.stdout + result.stderr


def test_invalid_identity_policy_fails_closed(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    git(root, "config", "leakSweep.allowedIdentity", "invalid-private-fixture")
    result = run_scan(root, "--staged")
    assert result.returncode == 2
    assert "invalid-private-fixture" not in result.stderr


def test_optional_private_patterns_allow_public_builtin_only_use(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    (root / "safe.txt").write_text("ordinary public text\n", encoding="utf-8")
    git(root, "add", "safe.txt")
    result = run_scan(root, "--staged")
    assert result.returncode == 0
    assert "custom=0" in result.stdout


def test_staged_mode_reads_index_not_worktree(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    marker = "Index" + "OnlyMarker"
    write_private_patterns(root, marker)
    target = root / "note.txt"
    target.write_text(marker, encoding="utf-8")
    git(root, "add", "note.txt")
    target.write_text("clean worktree replacement\n", encoding="utf-8")

    result = run_scan(root, "--staged")

    assert result.returncode == 1
    assert marker not in result.stdout + result.stderr


def test_history_finds_deleted_blob_and_historical_path(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    marker = "Deleted" + "HistoryMarker"
    write_private_patterns(root, marker)
    target = root / f"{marker}.txt"
    target.write_text(marker, encoding="utf-8")
    commit_all(root)
    target.unlink()
    commit_all(root)

    result = run_scan(root, "--history")

    assert result.returncode == 1
    assert "category=privacy type=custom-1 count=2" in result.stdout
    assert marker not in result.stdout + result.stderr


def test_history_scans_commit_identity_ref_and_annotated_tag_metadata(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    marker = "Metadata" + "Marker"
    write_private_patterns(root, marker)
    git(root, "config", "user.name", marker)
    commit_all(root, f"message containing {marker}")
    git(root, "tag", "-a", f"tag-{marker}", "-m", f"annotation {marker}")

    result = run_scan(root, "--history")

    assert result.returncode == 1
    count_match = re.search(r"category=privacy type=custom-1 count=(\d+)", result.stdout)
    assert count_match is not None
    assert int(count_match[1]) >= 4
    assert marker not in result.stdout + result.stderr


def test_pre_push_scans_raw_sha_range_and_deleted_blob(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    marker = "Outgoing" + "Marker"
    write_private_patterns(root, marker)
    target = root / "temporary.txt"
    target.write_text(marker, encoding="utf-8")
    commit_all(root)
    target.unlink()
    tip = commit_all(root)
    zeros = "0" * len(tip)
    updates = tmp_path / "updates"
    updates.write_text(f"HEAD {tip} refs/heads/new {zeros}\n", encoding="utf-8")

    result = run_scan(root, "--pre-push", "--updates-file", str(updates))

    assert result.returncode == 1
    assert marker not in result.stdout + result.stderr
    assert str(updates) not in result.stdout + result.stderr


def test_pre_push_scans_every_update_not_only_first(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    base = commit_all(root)
    marker = "Second" + "UpdateMarker"
    write_private_patterns(root, marker)
    git(root, "checkout", "-q", "-b", "clean-branch")
    clean = commit_all(root, "clean update")
    git(root, "checkout", "-q", "-b", "finding-branch", base)
    finding = commit_all(root, marker)
    updates = tmp_path / "updates"
    updates.write_text(
        f"refs/heads/clean-branch {clean} refs/heads/clean {base}\n"
        f"refs/heads/finding-branch {finding} refs/heads/finding {base}\n",
        encoding="utf-8",
    )

    result = run_scan(root, "--pre-push", "--updates-file", str(updates))

    assert result.returncode == 1
    assert marker not in result.stdout + result.stderr


def test_revision_paths_include_every_name_for_reused_blob_and_deleted_names(
    tmp_path: Path,
) -> None:
    root = init_repo(tmp_path / "repo")
    base = commit_all(root)
    marker = "Reused" + "PathMarker"
    source = root / f"gone-{marker}.txt"
    source.write_text("shared safe contents\n", encoding="utf-8")
    first = commit_all(root)
    renamed = root / f"renamed-{marker}.txt"
    source.rename(renamed)
    (root / f"alias-one-{marker}.txt").write_bytes(renamed.read_bytes())
    (root / f"alias-two-{marker}.txt").write_bytes(renamed.read_bytes())
    second = commit_all(root)
    for path in root.glob(f"*{marker}.txt"):
        path.unlink()
    tip = commit_all(root)

    chunks = leak_sweep._revision_chunks(root, [f"{base}..{tip}"])
    decoded = [leak_sweep.privacy_text(chunk, set()) for chunk in chunks]

    assert first != second != tip
    assert decoded.count(f"gone-{marker}.txt") == 1
    assert decoded.count(f"renamed-{marker}.txt") == 1
    assert decoded.count(f"alias-one-{marker}.txt") == 1
    assert decoded.count(f"alias-two-{marker}.txt") == 1
    assert decoded.count("shared safe contents\n") == 1


def test_reused_blob_paths_reach_credential_scanner(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    base = commit_all(root)
    token = "filename" + "-credential-fixture"
    source = root / "original.txt"
    source.write_text("shared safe contents\n", encoding="utf-8")
    commit_all(root)
    source.rename(root / f"renamed-{token}.txt")
    tip = commit_all(root)
    updates = tmp_path / "updates"
    updates.write_text(f"HEAD {tip} refs/heads/main {base}\n", encoding="utf-8")
    executable = fake_gitleaks(tmp_path)

    result = run_scan(
        root,
        "--pre-push",
        "--updates-file",
        str(updates),
        "--credentials",
        "--gitleaks-executable",
        str(executable),
        env={"FAKE_SECRET": token},
    )

    assert result.returncode == 1
    assert "category=credential type=generic count=1" in result.stdout
    assert token not in result.stdout + result.stderr


@pytest.mark.parametrize("target_kind", ["blob", "tree"])
def test_noncommit_tag_targets_are_scanned(tmp_path: Path, target_kind: str) -> None:
    root = init_repo(tmp_path / "repo")
    marker = "Noncommit" + "PrivateMarker"
    write_private_patterns(root, marker)
    payload = marker.encode() if target_kind == "blob" else b"safe content"
    blob = git(root, "hash-object", "-w", "--stdin", input_data=payload).stdout.decode().strip()
    target = blob
    if target_kind == "tree":
        target = (
            git(
                root,
                "mktree",
                input_data=f"100644 blob {blob}\t{marker}.txt\n".encode(),
            )
            .stdout.decode()
            .strip()
        )
    git(root, "tag", "-a", "fixture-tag", target, "-m", "safe annotation")
    tip = git(root, "rev-parse", "fixture-tag").stdout.decode().strip()
    updates = tmp_path / "updates"
    updates.write_text(f"refs/tags/fixture-tag {tip} refs/tags/fixture-tag {'0' * 40}\n")
    for args in (("--history",), ("--pre-push", "--updates-file", str(updates))):
        result = run_scan(root, *args)
        assert result.returncode == 1
        assert marker not in result.stdout + result.stderr


def test_commit_message_credentials_reach_gitleaks_stdin(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    token = "credential" + "-fixture-value"
    tip = commit_all(root, f"metadata {token}")
    updates = tmp_path / "updates"
    updates.write_text(f"HEAD {tip} refs/heads/new {'0' * len(tip)}\n", encoding="utf-8")
    executable = fake_gitleaks(tmp_path)

    result = run_scan(
        root,
        "--pre-push",
        "--updates-file",
        str(updates),
        "--credentials",
        "--gitleaks-executable",
        str(executable),
        env={"FAKE_SECRET": token},
    )

    assert result.returncode == 1
    assert "category=credential type=generic count=1" in result.stdout
    assert token not in result.stdout + result.stderr


def test_text_file_scans_privacy_and_credentials_without_posting(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    marker = "Prose" + "IdentityMarker"
    token = "prose" + "-credential-fixture"
    write_private_patterns(root, marker)
    prose = tmp_path / "publication.txt"
    prose.write_text(f"title {marker}\nbody {token}\n", encoding="utf-8")
    executable = fake_gitleaks(tmp_path)

    result = run_scan(
        root,
        "--text-file",
        str(prose),
        "--gitleaks-executable",
        str(executable),
        env={"FAKE_SECRET": token},
    )

    assert result.returncode == 1
    assert "category=privacy" in result.stdout
    assert "category=credential" in result.stdout
    assert marker not in result.stdout + result.stderr
    assert token not in result.stdout + result.stderr
    assert str(prose) not in result.stdout + result.stderr


def test_diagnostics_are_safe_category_type_and_count_only(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    marker = "Repeated" + "PrivateMarker"
    write_private_patterns(root, marker)
    sensitive_name = f"{marker}-filename.txt"
    (root / sensitive_name).write_text(f"{marker}\n{marker}\n", encoding="utf-8")
    git(root, "add", sensitive_name)

    result = run_scan(root, "--staged")

    assert result.returncode == 1
    assert "finding category=privacy type=custom-1 count=3" in result.stdout
    assert "hash=" not in result.stdout
    assert marker not in result.stdout + result.stderr
    assert sensitive_name not in result.stdout + result.stderr
    assert all(
        line.startswith("leak-sweep:")
        for line in (result.stdout + result.stderr).splitlines()
        if line
    )


def test_credential_dictionary_values_and_hashes_never_reach_diagnostics(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    prose = tmp_path / "safe.txt"
    prose.write_text("ordinary text\n", encoding="utf-8")
    executable = fake_gitleaks(tmp_path)
    secret = "synthetic" + "-dictionary-secret"
    arbitrary_rule = "private detector supplied rule"
    payload = {
        "RuleID": arbitrary_rule,
        "Secret": secret,
        "Match": f"prefix {secret}",
        "File": "/synthetic/private/location",
    }

    result = run_scan(
        root,
        "--text-file",
        str(prose),
        "--gitleaks-executable",
        str(executable),
        env={"FAKE_SECRET": "unused", "FAKE_REPORT_JSON": json.dumps([payload])},
    )

    diagnostics = result.stdout + result.stderr
    assert result.returncode == 1
    assert "finding category=credential type=generic count=1" in result.stdout
    assert "hash=" not in diagnostics
    for value in payload.values():
        assert value not in diagnostics
        digest = hashlib.sha256(value.encode()).hexdigest()
        assert digest not in diagnostics
        assert digest[:16] not in diagnostics


def test_non_utf8_publication_input_fails_closed(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    (root / "binary.dat").write_bytes(b"valid-prefix\xffinvalid")
    git(root, "add", "binary.dat")
    result = run_scan(root, "--staged")
    assert result.returncode == 2
    assert result.stderr.strip() == "leak-sweep: error=non-UTF-8 publication input rejected"


def test_missing_or_broken_gitleaks_fails_without_raw_subprocess_output(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    prose = tmp_path / "safe.txt"
    prose.write_text("ordinary text\n", encoding="utf-8")
    broken = tmp_path / "broken-scanner"
    raw_error = "scanner" + "-private-error"
    broken.write_text(f"#!/bin/sh\nprintf '%s\\n' '{raw_error}' >&2\nexit 9\n", encoding="utf-8")
    broken.chmod(0o755)

    missing_input_name = "private" + "-missing-publication.txt"
    unreadable = run_scan(root, "--text-file", str(tmp_path / missing_input_name))
    missing = run_scan(root, "--text-file", str(prose), "--gitleaks-executable", "missing-tool")
    failed = run_scan(root, "--text-file", str(prose), "--gitleaks-executable", str(broken))

    assert unreadable.returncode == 2
    assert missing_input_name not in unreadable.stdout + unreadable.stderr
    assert missing.returncode == 2
    assert failed.returncode == 2
    assert raw_error not in failed.stdout + failed.stderr
    assert str(broken) not in failed.stdout + failed.stderr


def test_safe_text_file_passes(tmp_path: Path) -> None:
    root = init_repo(tmp_path / "repo")
    prose = tmp_path / "safe.txt"
    prose.write_text("ordinary publication prose\n", encoding="utf-8")
    executable = fake_gitleaks(tmp_path)
    result = run_scan(
        root,
        "--text-file",
        str(prose),
        "--gitleaks-executable",
        str(executable),
        env={"FAKE_SECRET": "not-present-fixture"},
    )
    assert result.returncode == 0
    assert result.stdout.endswith("leak-sweep: clean\n")


def test_fake_gitleaks_report_contract_is_json(tmp_path: Path) -> None:
    """Keep the fixture aligned with the subset of the gitleaks JSON contract we consume."""
    executable = fake_gitleaks(tmp_path)
    report = tmp_path / "report.json"
    token = "contract-fixture"
    result = subprocess.run(
        [str(executable), "stdin", "--report-path", str(report)],
        input=token.encode(),
        env={**os.environ, "FAKE_SECRET": token},
    )
    assert result.returncode == 1
    assert json.loads(report.read_text(encoding="utf-8"))[0]["RuleID"] == "fixture-rule"


def test_scan_error_is_not_a_public_data_container() -> None:
    assert issubclass(leak_sweep.ScanError, Exception)
    with pytest.raises(leak_sweep.ScanError):
        leak_sweep._decode(b"\xff")
