from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATE_DIR = Path(".wikic")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def slugify(value: str) -> str:
    out: list[str] = []
    prev_dash = False
    for ch in value.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-") or "source"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def state_path(vault: Path) -> Path:
    return vault / STATE_DIR


def candidate_dir(vault: Path) -> Path:
    return state_path(vault) / "candidates"


def accepted_dir(vault: Path) -> Path:
    return state_path(vault) / "accepted"


def sources_dir(vault: Path) -> Path:
    return state_path(vault) / "sources"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def candidate_path(vault: Path, candidate_id: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", candidate_id) is None:
        raise ValueError(candidate_id)
    path = candidate_dir(vault) / f"{candidate_id}.json"
    resolved = path.resolve()
    try:
        resolved.relative_to(candidate_dir(vault).resolve())
    except ValueError:
        raise ValueError(candidate_id) from None
    return resolved


def candidate_path_or_none(vault: Path, candidate_id: str) -> Path | None:
    try:
        return candidate_path(vault, candidate_id)
    except ValueError:
        return None


def unsafe_reason(vault: Path, target_path: str | None) -> str | None:
    if not target_path:
        return "missing_target_path"
    target = Path(target_path)
    if target.is_absolute():
        return "unsafe_target_path"
    if any(part in {"..", ""} for part in target.parts):
        return "unsafe_target_path"
    resolved = (vault / target).resolve()
    try:
        resolved.relative_to(vault.resolve())
    except ValueError:
        return "unsafe_target_path"
    if target.parts and target.parts[0].startswith("."):
        return "unsafe_target_path"
    return None


def validate_candidate(vault: Path, path: Path) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    try:
        data = load_json(path)
    except Exception as exc:
        return [{"code": "invalid_json", "path": str(path), "message": str(exc)}]
    cid = data.get("id")
    if not cid:
        issues.append({"code": "missing_id", "path": str(path)})
    elif path.stem != cid:
        issues.append({"code": "id_filename_mismatch", "path": str(path), "id": cid})
    reason = unsafe_reason(vault, data.get("target_path"))
    if reason:
        issues.append({"code": reason, "path": str(path), "target_path": data.get("target_path")})
    if not isinstance(data.get("content"), str) or not data.get("content"):
        issues.append({"code": "missing_content", "path": str(path)})
    return issues


def ingest_source(vault: Path, source: Path) -> tuple[dict[str, Any], int]:
    src = source.expanduser().resolve()
    if not src.exists() or not src.is_file():
        return {
            "ok": False,
            "action": "ingest",
            "error": {"code": "source_missing", "message": str(src)},
        }, 2

    data = src.read_bytes()
    digest = sha256_bytes(data)
    source_id = f"{slugify(src.stem)}-{digest[:12]}"
    out_dir = sources_dir(vault)
    out_dir.mkdir(parents=True, exist_ok=True)
    captured = out_dir / f"{source_id}{src.suffix or '.md'}"
    manifest = out_dir / f"{source_id}.json"
    if not captured.exists() or captured.read_bytes() != data:
        captured.write_bytes(data)
    meta = {
        "source_id": source_id,
        "original_path": str(src),
        "captured_path": str(captured.relative_to(vault)),
        "sha256": digest,
        "bytes": len(data),
        "ingested_at": now_iso(),
    }
    manifest.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "action": "ingest", "vault": str(vault), **meta}, 0


def list_candidates(vault: Path) -> dict[str, Any]:
    candidates = []
    for path in sorted(candidate_dir(vault).glob("*.json")):
        try:
            data = load_json(path)
            candidates.append(
                {
                    "id": data.get("id", path.stem),
                    "target_path": data.get("target_path"),
                    "source_ids": data.get("source_ids", []),
                    "path": str(path.relative_to(vault)),
                }
            )
        except Exception as exc:
            candidates.append({"id": path.stem, "path": str(path), "error": str(exc)})
    return {"ok": True, "action": "review.list", "vault": str(vault), "candidates": candidates}


def show_candidate(vault: Path, candidate_id: str) -> tuple[dict[str, Any], int]:
    path = candidate_path_or_none(vault, candidate_id)
    if path is None or not path.exists():
        return {
            "ok": False,
            "action": "review.show",
            "error": {"code": "candidate_missing", "message": candidate_id},
        }, 2
    return {
        "ok": True,
        "action": "review.show",
        "vault": str(vault),
        "candidate": load_json(path),
    }, 0


def accept_candidate(
    vault: Path,
    candidate_id: str,
    *,
    dry_run: bool,
    apply: bool,
) -> tuple[dict[str, Any], int]:
    if dry_run == apply:
        return {
            "ok": False,
            "action": "review.accept",
            "error": {
                "code": "choose_dry_run_or_apply",
                "message": "Pass exactly one of --dry-run or --apply",
            },
        }, 2
    path = candidate_path_or_none(vault, candidate_id)
    if path is None or not path.exists():
        return {
            "ok": False,
            "action": "review.accept",
            "error": {"code": "candidate_missing", "message": candidate_id},
        }, 2
    data = load_json(path)
    reason = unsafe_reason(vault, data.get("target_path"))
    if reason:
        return {
            "ok": False,
            "action": "review.accept",
            "error": {"code": reason, "message": f"Unsafe target_path: {data.get('target_path')}"},
        }, 2
    if not isinstance(data.get("content"), str) or not data["content"]:
        return {
            "ok": False,
            "action": "review.accept",
            "error": {"code": "missing_content", "message": candidate_id},
        }, 2

    target = vault / data["target_path"]
    payload: dict[str, Any] = {
        "ok": True,
        "action": "review.accept",
        "vault": str(vault),
        "candidate_id": candidate_id,
        "dry_run": bool(dry_run),
        "would_write": [data["target_path"]],
    }
    if dry_run:
        return payload, 0

    target.parent.mkdir(parents=True, exist_ok=True)
    before_sha = sha256_bytes(target.read_bytes()) if target.exists() else None
    target.write_text(data["content"], encoding="utf-8")
    accepted_dir(vault).mkdir(parents=True, exist_ok=True)
    archived = accepted_dir(vault) / path.name
    archive_data = dict(data)
    archive_data["accepted_at"] = now_iso()
    archive_data["target_sha256_before"] = before_sha
    archive_data["target_sha256_after"] = sha256_bytes(target.read_bytes())
    archived.write_text(json.dumps(archive_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.unlink()
    payload.update(
        {
            "written": [data["target_path"]],
            "accepted_path": str(archived.relative_to(vault)),
        }
    )
    return payload, 0


def lint_candidates(vault: Path) -> tuple[dict[str, Any], int]:
    issues: list[dict[str, Any]] = []
    for path in sorted(candidate_dir(vault).glob("*.json")):
        issues.extend(validate_candidate(vault, path))
    payload = {"ok": not issues, "action": "lint", "vault": str(vault), "issues": issues}
    return payload, 0 if not issues else 1
