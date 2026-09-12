from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "wikic", *args, "--root", str(tmp_path)],
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_doctor_json_returns_report_and_exit_code(tmp_path: Path) -> None:
    write(tmp_path / "a.md", "# A\n[[missing]]")

    result = run_cli(tmp_path, "doctor", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["issue_count"] == 1
    assert payload["issues"][0]["code"] == "WK001"


def test_cli_repair_plan_json_returns_review_required_plan(tmp_path: Path) -> None:
    write(tmp_path / "a.md", "# A\n[[wiki-c]]")
    write(tmp_path / "b.md", "# B\n[[wiki-c]]")
    write(tmp_path / "tools" / "wikic.md", "# Wikic\n")

    result = run_cli(tmp_path, "repair-plan", "--ignore-orphans", "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["operations"][0]["action"] == "retarget_wikilinks"
    assert payload["operations"][0]["status"] == "review_required"
    assert payload["operations"][0]["replacement_target"] == "tools/wikic"


def test_cli_repair_plan_patch_preview_prints_unified_diff(tmp_path: Path) -> None:
    write(tmp_path / "a.md", "# A\n[[wiki-c|Wikic]]\n")
    write(tmp_path / "b.md", "# B\n[[wiki-c]]\n")
    write(tmp_path / "tools" / "wikic.md", "# Wikic\n")

    result = run_cli(tmp_path, "repair-plan", "--ignore-orphans", "--patch-preview")

    assert result.returncode == 0
    assert "--- a/a.md" in result.stdout
    assert "+++ b/a.md" in result.stdout
    assert "-[[wiki-c|Wikic]]" in result.stdout
    assert "+[[tools/wikic|Wikic]]" in result.stdout


def test_cli_summary_json_returns_compact_snapshot(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Home\n[[tools/wikic]]\n")
    write(tmp_path / "tools" / "wikic.md", "# Wikic\n")

    result = run_cli(tmp_path, "summary", "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["catalog"]["page_count"] == 2
    assert payload["doctor"]["issue_count"] == 0
    assert payload["graph"]["resolved_edges"] == 1
    assert "okf_readiness" not in payload


def test_cli_summary_profile_okf_json_returns_readiness(tmp_path: Path) -> None:
    write(tmp_path / "missing-type.md", "---\ntitle: Missing Type\n---\n# Missing Type\n")

    result = run_cli(tmp_path, "summary", "--profile", "okf-v0.2", "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["okf_readiness"]["conformant"] is False
    assert payload["okf_readiness"]["invalid_type_pages"] == [
        {"path": "missing-type.md", "type": "None"}
    ]


def test_cli_doctor_profile_okf_reports_profile_diagnostics(tmp_path: Path) -> None:
    write(tmp_path / "missing-type.md", "---\ntitle: Missing Type\n---\n# Missing Type\n")

    result = run_cli(
        tmp_path,
        "doctor",
        "--ignore-orphans",
        "--profile",
        "okf-v0.2",
        "--json",
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["issues"][0]["code"] == "WK011"


def test_cli_uses_configured_okf_without_profile_flag(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        json.dumps({"okf": {"enabled": True, "version": "0.2", "exclude": []}}),
    )
    write(tmp_path / "missing-type.md", "---\ntitle: Missing Type\n---\n# Missing Type\n")

    summary = run_cli(tmp_path, "summary", "--json")
    doctor = run_cli(tmp_path, "doctor", "--ignore-orphans", "--json")

    assert summary.returncode == 0
    assert json.loads(summary.stdout)["okf_readiness"]["invalid_type_count"] == 1
    assert doctor.returncode == 1
    assert json.loads(doctor.stdout)["issues"][0]["code"] == "WK011"


def test_cli_catalog_writes_json_artifact(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Home\n")

    result = run_cli(tmp_path, "catalog")

    assert result.returncode == 0
    output = tmp_path / ".wikic" / "catalog.json"
    assert output.exists()
    assert json.loads(output.read_text())["page_count"] == 1


def test_cli_llms_writes_llms_txt(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Home\n")

    result = run_cli(tmp_path, "llms")

    assert result.returncode == 0
    assert (tmp_path / "llms.txt").read_text(encoding="utf-8").startswith("# ")
