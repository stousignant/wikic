import importlib.util
import re
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "leak_sweep.py"
SPEC = importlib.util.spec_from_file_location("leak_sweep", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
leak_sweep = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = leak_sweep
SPEC.loader.exec_module(leak_sweep)


def test_builtin_windows_pattern_matches_single_backslashes() -> None:
    """The literal user path uses single backslashes; the regex must match it."""
    pattern = leak_sweep.BUILTIN_PATTERNS["windows-user-path"]
    windows_path = "C:" + r"\Users\alice\Documents\notes.md"
    assert re.search(pattern, windows_path)
    assert not re.search(pattern, "https://example.com")


def test_builtin_home_pattern_matches_common_layouts() -> None:
    pattern = leak_sweep.BUILTIN_PATTERNS["absolute-home-path"]
    linux_path = "/" + "home/alice/notes.md"
    mac_path = "/" + "Users/alice/Documents/notes.md"
    assert re.search(pattern, linux_path)
    assert re.search(pattern, mac_path)
    assert not re.search(pattern, "https://example.com/homepage")


def test_redact_masks_the_sensitive_span() -> None:
    private_path = "/" + "home/alice/secret.txt"
    line = f"contact alice at {private_path}"
    match = re.search(leak_sweep.BUILTIN_PATTERNS["absolute-home-path"], line)
    redacted = leak_sweep.redact(line, match)
    assert private_path not in redacted
    assert "<REDACTED>" in redacted


def test_load_patterns_merges_runtime_entries(tmp_path) -> None:
    env = {"LEAK_PATTERNS": "private-name\n# comment line\n\n"}
    patterns_file = tmp_path / "patterns"
    patterns_file.write_text("internal-tool\n", encoding="utf-8")

    import os

    old = os.environ.get("LEAK_PATTERNS")
    os.environ.update(env)
    try:
        patterns = leak_sweep.load_patterns(patterns_file)
    finally:
        if old is None:
            os.environ.pop("LEAK_PATTERNS", None)
        else:
            os.environ["LEAK_PATTERNS"] = old

    assert "private-name" in patterns.values()
    assert "internal-tool" in patterns.values()
    assert "# comment line" not in patterns.values()
    assert leak_sweep.BUILTIN_PATTERNS["absolute-home-path"] in patterns.values()


def test_scan_files_reports_findings_and_respects_excludes(tmp_path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    private_path = "/" + "home/alice/notes.md"
    (root / "src" / "a.md").write_text(f"see {private_path}", encoding="utf-8")
    (root / "skip.md").write_text(f"also {private_path}", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "src/a.md", "skip.md"], cwd=root, check=True)

    compiled = {name: re.compile(pattern) for name, pattern in leak_sweep.BUILTIN_PATTERNS.items()}
    skip = {(root / "skip.md").resolve()}

    findings = leak_sweep.scan_files(root, compiled, skip)

    locations = {finding.location for finding in findings}
    assert locations == {"src/a.md"}
    assert findings[0].pattern == "absolute-home-path"


def test_scan_messages_extracts_commit_text(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    private_path = "/" + "home/alice/leak.md"
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.test",
            "commit",
            "--allow-empty",
            "-m",
            f"note {private_path}",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    compiled = {name: re.compile(pattern) for name, pattern in leak_sweep.BUILTIN_PATTERNS.items()}

    findings = leak_sweep.scan_messages(root, compiled)

    assert any(finding.pattern == "absolute-home-path" for finding in findings)
