from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "wikic", *args],
        text=True,
        capture_output=True,
        check=False,
    )


def parse_json(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stdout, result.stderr
    return json.loads(result.stdout)


def test_ingest_captures_source_into_wikic_state_with_manifest(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    source = tmp_path / "source.md"
    vault.mkdir()
    source.write_text("# Source\n\nBody\n", encoding="utf-8")

    result = run_cli("ingest", str(source), "--vault", str(vault), "--json")

    assert result.returncode == 0
    payload = parse_json(result)
    assert payload["ok"] is True
    source_id = payload["source_id"]
    captured = vault / ".wikic" / "sources" / f"{source_id}.md"
    manifest = vault / ".wikic" / "sources" / f"{source_id}.json"
    assert captured.exists()
    assert manifest.exists()
    assert "Body" in captured.read_text(encoding="utf-8")
    assert json.loads(manifest.read_text(encoding="utf-8"))["sha256"] == payload["sha256"]


def test_review_list_show_and_accept_candidate(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    cand_dir = vault / ".wikic" / "candidates"
    cand_dir.mkdir(parents=True)
    (cand_dir / "cand-1.json").write_text(
        json.dumps(
            {
                "id": "cand-1",
                "target_path": "concepts/example.md",
                "content": "# Example\n\nCreated by candidate.\n",
                "source_ids": ["src-1"],
            }
        ),
        encoding="utf-8",
    )

    listed = run_cli("review", "list", "--vault", str(vault), "--json")
    shown = run_cli("review", "show", "cand-1", "--vault", str(vault), "--json")
    dry = run_cli(
        "review",
        "accept",
        "cand-1",
        "--vault",
        str(vault),
        "--dry-run",
        "--json",
    )
    applied = run_cli(
        "review",
        "accept",
        "cand-1",
        "--vault",
        str(vault),
        "--apply",
        "--json",
    )

    assert listed.returncode == 0
    assert parse_json(listed)["candidates"][0]["id"] == "cand-1"
    assert shown.returncode == 0
    assert parse_json(shown)["candidate"]["target_path"] == "concepts/example.md"
    assert dry.returncode == 0
    assert parse_json(dry)["would_write"] == ["concepts/example.md"]
    assert applied.returncode == 0
    assert (vault / "concepts" / "example.md").read_text(encoding="utf-8").startswith("# Example")
    assert not (cand_dir / "cand-1.json").exists()
    assert (vault / ".wikic" / "accepted" / "cand-1.json").exists()


def test_review_accept_rejects_path_escape(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    cand_dir = vault / ".wikic" / "candidates"
    cand_dir.mkdir(parents=True)
    (cand_dir / "evil.json").write_text(
        json.dumps({"id": "evil", "target_path": "../outside.md", "content": "bad"}),
        encoding="utf-8",
    )

    result = run_cli("review", "accept", "evil", "--vault", str(vault), "--apply", "--json")

    assert result.returncode == 2
    payload = parse_json(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unsafe_target_path"
    assert not (tmp_path / "outside.md").exists()


def test_review_accept_rejects_hidden_target_directory(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    cand_dir = vault / ".wikic" / "candidates"
    cand_dir.mkdir(parents=True)
    (cand_dir / "hidden.json").write_text(
        json.dumps({"id": "hidden", "target_path": ".private/secret.md", "content": "bad"}),
        encoding="utf-8",
    )

    result = run_cli("review", "accept", "hidden", "--vault", str(vault), "--apply", "--json")

    assert result.returncode == 2
    assert parse_json(result)["error"]["code"] == "unsafe_target_path"
    assert not (vault / ".private" / "secret.md").exists()


def test_review_accept_rejects_candidate_id_path_escape(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    cand_dir = vault / ".wikic" / "candidates"
    cand_dir.mkdir(parents=True)
    outside = tmp_path / "victim.json"
    outside.write_text(
        json.dumps({"id": "../../victim", "target_path": "safe.md", "content": "bad"}),
        encoding="utf-8",
    )

    result = run_cli("review", "accept", "../../victim", "--vault", str(vault), "--apply", "--json")

    assert result.returncode == 2
    payload = parse_json(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "candidate_missing"
    assert outside.exists()
    assert not (vault / "safe.md").exists()


def test_review_show_rejects_absolute_candidate_id(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    victim = tmp_path / "victim.json"
    victim.write_text(json.dumps({"id": "victim"}), encoding="utf-8")

    result = run_cli("review", "show", str(victim.with_suffix("")), "--vault", str(vault), "--json")

    assert result.returncode == 2
    assert parse_json(result)["error"]["code"] == "candidate_missing"
    assert victim.exists()


def test_lint_candidates_reports_invalid_candidate_shape_and_path(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    cand_dir = vault / ".wikic" / "candidates"
    cand_dir.mkdir(parents=True)
    (cand_dir / "bad.json").write_text(
        json.dumps({"id": "bad", "target_path": "/absolute.md"}),
        encoding="utf-8",
    )

    result = run_cli("lint", "--candidates", "--vault", str(vault), "--json")

    assert result.returncode == 1
    codes = {issue["code"] for issue in parse_json(result)["issues"]}
    assert "missing_content" in codes
    assert "unsafe_target_path" in codes


def test_doctor_can_require_canonical_vault_files(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".wikic").mkdir()
    (vault / ".wikic" / "config.json").write_text(
        '{"required_root_files": ["SCHEMA.md", "index.md", "log.md"]}',
        encoding="utf-8",
    )
    (vault / "index.md").write_text("# Index\n", encoding="utf-8")

    result = run_cli("doctor", "--root", str(vault), "--require-vault-files", "--json")

    assert result.returncode == 1
    payload = parse_json(result)
    missing = {issue["path"] for issue in payload["issues"] if issue["code"] == "WK003"}
    assert missing == {"SCHEMA.md", "log.md"}
