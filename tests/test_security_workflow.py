"""Exercise the real CI scanner commands without credentials or network calls."""

import os
import subprocess
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/security.yml"


def scanner_command(name: str) -> str:
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {name}")
    assert lines[start + 1].strip() == "run: |"
    script = []
    for line in lines[start + 2 :]:
        if line.strip() and not line.startswith("          "):
            break
        script.append(line[10:])
    return "\n".join(script)


@pytest.mark.parametrize(
    ("binary", "step"),
    [
        ("gitleaks", "gitleaks (every ref, full history)"),
        ("trufflehog", "trufflehog (verified and unverifiable)"),
    ],
)
@pytest.mark.parametrize(("status", "output"), [(0, ""), (1, "fixture"), (2, "fixture")])
def test_scanner_logs_are_safe(tmp_path, binary, step, status, output):
    marker = "sensitive" + "-diagnostic-fixture"
    executable = tmp_path / binary
    executable.write_text(
        "#!/bin/sh\n"
        + (f"printf '%s\\n' '{marker}'\n" if output else "")
        + f"printf '%s\\n' '{marker}' >&2\n"
        + f"exit {status}\n"
    )
    executable.chmod(0o700)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", TMPDIR=str(temporary))
    result = subprocess.run(
        ["bash", "-c", scanner_command(step)], env=env, capture_output=True, text=True
    )
    assert result.returncode == status
    assert marker not in result.stdout + result.stderr
    assert not list(temporary.iterdir())


def test_trufflehog_nonempty_success_is_not_clean(tmp_path):
    executable = tmp_path / "trufflehog"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' 'synthetic-finding'\nexit 0\n")
    executable.chmod(0o700)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", TMPDIR=str(tmp_path))
    result = subprocess.run(
        ["bash", "-c", scanner_command("trufflehog (verified and unverifiable)")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "synthetic-finding" not in result.stdout + result.stderr
    assert set(tmp_path.iterdir()) == {executable}
