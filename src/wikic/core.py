from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher, unified_diff
from pathlib import Path, PurePosixPath
from typing import Any

LINK_RE = re.compile(r"(?<!!)\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
LINK_OCCURRENCE_RE = re.compile(r"(?<!!)\[\[([^\]|#]+)((?:#[^\]|]+)?(?:\|[^\]]+)?)\]\]")
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
WORD_RE = re.compile(r"\b\w+\b")
ACTION_MARKER_RE = re.compile(r"(?im)(?:^\s*(?:[-*]\s*)?(?:TODO|FIXME|ACTION|NEXT)\b|- \[ \])")
STALE_MARKER_RE = re.compile(r"(?i)\b(?:stale|outdated|deprecated|obsolete|needs refresh)\b")
TIMELINE_SENTINEL_RE = re.compile(r"(?i)<!--\s*timeline\s*-->")
TIMELINE_SECTION_RE = re.compile(r"(?im)^##\s*(Timeline|History)\s*$")
TIMELINE_ENTRY_BULLET_RE = re.compile(
    r"(?m)^\s*[-*]\s+\*\*\d{4}-\d{2}-\d{2}\*\*\s*\|\s*.+?\s*[—–-]\s*.+$"
)
TIMELINE_ENTRY_HEADING_RE = re.compile(r"(?m)^###\s+\d{4}-\d{2}-\d{2}\s")
TIMELINE_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
TIMELINE_ELIGIBLE_TYPES = {
    "tool",
    "project",
    "person",
    "company",
    "profile",
    "system-design",
    "priorities",
    "identity",
}
TIMELINE_PATH_PREFIXES = ("pai/",)
TIMELINE_TOP_MISSING_LIMIT = 20
LARGE_PAGE_WORD_THRESHOLD = 1000
IGNORED_DIRS = {".git", ".hg", ".svn", ".wikic", "node_modules", ".venv", "venv"}
DEFAULT_EXCLUDE_PATTERNS = [
    "archive/**",
    "archives/**",
    "raw/**",
    "generated/**",
    "tmp/**",
]
OKF_PROFILE = "okf-compatible"
OKF_ALLOWED_TYPES = {
    "tool",
    "concept",
    "project",
    "person",
    "company",
    "comparison",
    "report",
    "source-note",
    "meeting",
    "guide",
    "index",
    "decision",
    "reference",
    "repo-intel",
    # Declared in the vault's own SCHEMA.md type enum.
    "idea",
    "note",
    "raw",
    "standard",
}
OKF_ALLOWED_FRONTMATTER_FIELDS = {
    "aliases",
    "author",
    "created",
    "ingested_via",
    "last_reviewed",
    "owners",
    "source",
    "source_uri",
    "source_url",
    "sources",
    "status",
    "summary",
    "tags",
    "title",
    "type",
    "updated",
}
OKF_PROVENANCE_FIELDS = {"source", "sources", "source_url", "source_uri"}
OKF_CURATED_PREFIXES = (
    "wiki/",
    "projects/",
    "concepts/",
    "people/",
    "companies/",
    "tools/",
)
OKF_EXCLUDED_PATTERNS = [
    "archive/**",
    "archives/**",
    "raw/**",
    "generated/**",
    "tmp/**",
    "scratch/**",
    "planning/**",
    "plans/**",
    "reports/**",
]
OKF_SOURCE_DERIVED_PREFIXES = (
    "articles/",
    "comparisons/",
    "repo-intel/",
    "source-notes/",
    "sources/",
)
OKF_PROVENANCE_EXPECTED_TYPES = {
    "comparison",
    "repo-intel",
    "report",
    "source-note",
    "article",
}
OKF_TYPE_SUGGESTIONS = {
    "companies": "company",
    "comparisons": "comparison",
    "concepts": "concept",
    "decisions": "decision",
    "guides": "guide",
    "meetings": "meeting",
    "people": "person",
    "persons": "person",
    "projects": "project",
    "references": "reference",
    "reports": "report",
    "source-notes": "source-note",
    "sources": "source-note",
    "tools": "tool",
}


@dataclass(frozen=True)
class DoctorReport:
    ok: bool
    exit_code: int
    issue_count: int
    issues: list[dict[str, Any]]
    summary: dict[str, Any]
    work_queue: list[dict[str, Any]]
    work_queue_groups: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "issue_count": self.issue_count,
            "issues": self.issues,
            "summary": self.summary,
            "work_queue": self.work_queue,
            "work_queue_groups": self.work_queue_groups,
        }


def load_config(root: str | Path) -> dict[str, Any]:
    root_path = Path(root).resolve()
    default_config: dict[str, Any] = {
        "exclude": list(DEFAULT_EXCLUDE_PATTERNS),
        "naming_policy_exemptions": [],
        "generated_report_policy": {"rules": []},
        "okf_profile": {},
    }
    config_path = root_path / ".wikic" / "config.json"
    if not config_path.exists():
        return default_config

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"Invalid config shape in {config_path}: expected object")

    merged = dict(default_config)
    if "exclude" in raw:
        user_excludes = raw["exclude"]
        if not isinstance(user_excludes, list):
            raise ValueError(f"Invalid config value for 'exclude' in {config_path}: expected list")

        merged["exclude"] = _merge_excludes(default_config["exclude"], user_excludes)

    if "naming_policy_exemptions" in raw:
        exemptions = raw["naming_policy_exemptions"]
        if not isinstance(exemptions, list):
            raise ValueError(
                "Invalid config value for 'naming_policy_exemptions' "
                f"in {config_path}: expected list"
            )
        for pattern in exemptions:
            if not isinstance(pattern, str):
                raise ValueError(
                    "Invalid config value for 'naming_policy_exemptions' "
                    f"in {config_path}: expected list of strings"
                )
        merged["naming_policy_exemptions"] = exemptions

    if "generated_report_policy" in raw:
        merged["generated_report_policy"] = _validate_generated_report_policy(
            raw["generated_report_policy"], config_path
        )

    for key, value in raw.items():
        if key not in {"exclude", "naming_policy_exemptions", "generated_report_policy"}:
            merged[key] = value

    return merged


def _validate_generated_report_policy(value: Any, config_path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(
            f"Invalid config value for 'generated_report_policy' in {config_path}: expected object"
        )
    rules = value.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError(
            "Invalid config value for 'generated_report_policy.rules' "
            f"in {config_path}: expected list"
        )

    normalized_rules: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError(
                f"Invalid generated report policy rule in {config_path}: expected object"
            )
        path_glob = rule.get("path_glob")
        rollup = rule.get("rollup")
        require_frontmatter = rule.get("require_frontmatter", True)
        if not isinstance(path_glob, str) or not path_glob:
            raise ValueError(
                f"Invalid generated report policy rule in {config_path}: path_glob must be a string"
            )
        if rollup is not None and not isinstance(rollup, str):
            raise ValueError(
                f"Invalid generated report policy rule in {config_path}: rollup must be a string"
            )
        if not isinstance(require_frontmatter, bool):
            raise ValueError(
                "Invalid generated report policy rule in "
                f"{config_path}: require_frontmatter must be boolean"
            )
        normalized_rules.append(
            {
                "path_glob": path_glob,
                "rollup": rollup,
                "require_frontmatter": require_frontmatter,
            }
        )
    return {"rules": normalized_rules}


def _merge_excludes(default_excludes: list[str], user_excludes: list[Any]) -> list[str]:
    merged = []
    seen: set[str] = set()
    for pattern in default_excludes + user_excludes:
        if not isinstance(pattern, str):
            raise ValueError(f"Invalid exclude pattern: expected string, got {type(pattern)!r}")
        if pattern in seen:
            continue
        seen.add(pattern)
        merged.append(pattern)
    return merged


def normalize_slug(value: str) -> str:
    """Normalize a page reference to an Obsidian-friendly, slash-preserving slug."""
    value = value.strip().replace("\\", "/")
    if value.endswith(".md"):
        value = value[:-3]
    parts = []
    for part in value.split("/"):
        part = part.strip().lower()
        part = re.sub(r"[^a-z0-9\- _]+", "", part)
        part = re.sub(r"[\s_]+", "-", part)
        part = re.sub(r"-+", "-", part).strip("-")
        if part:
            parts.append(part)
    return "/".join(parts)


def markdown_files(root: Path, exclude_patterns: list[str] | None = None) -> tuple[list[Path], int]:
    root = root.resolve()
    files: list[Path] = []
    excluded: int = 0
    patterns = list(exclude_patterns or [])

    for path in root.rglob("*.md"):
        parent_parts = path.relative_to(root).parts[:-1]
        if any(
            part in IGNORED_DIRS or part.startswith(".") and part != "." for part in parent_parts
        ):
            continue
        rel_posix = path.relative_to(root).as_posix()
        if _matches_any_exclude_pattern(rel_posix, patterns):
            excluded += 1
            continue
        files.append(path)
    return sorted(files, key=lambda p: p.relative_to(root).as_posix()), excluded


def _matches_any_exclude_pattern(rel_path: str, patterns: list[str]) -> bool:
    posix_path = PurePosixPath(rel_path)
    for pattern in patterns:
        if pattern.endswith("/**"):
            prefix = pattern[:-3].rstrip("/")
            if rel_path == prefix or rel_path.startswith(f"{prefix}/"):
                return True
        if posix_path.match(pattern):
            return True
    return False


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    data: dict[str, Any] = {}
    current_key: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1].isspace():
            # Indented lines belong to the key above. Block sequences become a
            # list; anything else (nested maps, folded scalars) is left for the
            # frontmatter audit to flag rather than guessed at here.
            if current_key is not None and stripped.startswith("- "):
                item = _parse_scalar(stripped[2:].strip())
                existing = data.get(current_key)
                if isinstance(existing, list):
                    existing.append(item)
                else:
                    data[current_key] = [item]
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key.strip()
        data[current_key] = _parse_scalar(value.strip())
    return data, body


def _parse_scalar(value: str) -> Any:
    if value == "":
        return ""
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("\"'") for item in inner.split(",")]
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value


def page_title(slug: str, frontmatter: dict[str, Any], body: str) -> str:
    if title := frontmatter.get("title"):
        return str(title)
    if match := H1_RE.search(body):
        return match.group(1).strip()
    return slug.rsplit("/", 1)[-1].replace("-", " ").title()


def extract_links(body: str) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    for match in LINK_RE.finditer(body):
        slug = normalize_slug(match.group(1))
        if slug and slug not in seen:
            links.append(slug)
            seen.add(slug)
    return links


def build_catalog(root: str | Path) -> dict[str, Any]:
    root_path = Path(root).resolve()
    config = load_config(root_path)
    pages_files, excluded_count = markdown_files(
        root_path, exclude_patterns=config.get("exclude", [])
    )
    pages: dict[str, dict[str, Any]] = {}
    for path in pages_files:
        text = path.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(text)
        aliases = frontmatter.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        elif not isinstance(aliases, list):
            aliases = []
        rel = path.relative_to(root_path).with_suffix("").as_posix()
        slug = normalize_slug(rel)
        pages[slug] = {
            "slug": slug,
            "basename": slug.rsplit("/", 1)[-1],
            "aliases": aliases,
            "path": path.relative_to(root_path).as_posix(),
            "title": page_title(slug, frontmatter, body),
            "type": frontmatter.get("type"),
            "status": frontmatter.get("status"),
            "summary": frontmatter.get("summary"),
            "tags": frontmatter.get("tags", []),
            "outlinks": extract_links(body),
            "word_count": len(WORD_RE.findall(body)),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    return {
        "schema_version": 1,
        "root": str(root_path),
        "page_count": len(pages),
        "pages": pages,
        "summary": {
            "included_files": len(pages),
            "excluded_files": excluded_count,
            "exclude_patterns": config["exclude"],
        },
    }


def build_resolver_index(pages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    basename_index: dict[str, list[str]] = {}
    alias_index: dict[str, list[str]] = {}
    for slug, page in pages.items():
        basename = slug.rsplit("/", 1)[-1]
        basename_index.setdefault(basename, []).append(slug)
        for alias in page.get("aliases", []):
            normalized = normalize_slug(str(alias))
            if not normalized:
                continue
            alias_index.setdefault(normalized, []).append(slug)
    return {
        "exact": set(pages),
        "basename": {name: sorted(set(values)) for name, values in basename_index.items()},
        "alias": {name: sorted(set(values)) for name, values in alias_index.items()},
    }


def resolve_link(
    target: str,
    pages: dict[str, dict[str, Any]],
    index: dict[str, Any],
) -> dict[str, Any]:
    if target in index["exact"]:
        return {
            "resolved": True,
            "target": target,
            "resolution": "exact",
            "candidates": [],
        }

    basename_candidates = index["basename"].get(target, [])
    if len(basename_candidates) == 1:
        return {
            "resolved": True,
            "target": basename_candidates[0],
            "resolution": "basename",
            "candidates": [],
        }

    alias_candidates = index["alias"].get(target, [])
    if len(alias_candidates) == 1 and len(basename_candidates) == 0:
        return {
            "resolved": True,
            "target": alias_candidates[0],
            "resolution": "alias",
            "candidates": [],
        }

    candidates = sorted(set(basename_candidates) | set(alias_candidates))
    if candidates:
        return {
            "resolved": False,
            "target": None,
            "resolution": "ambiguous",
            "candidates": candidates,
        }

    return {
        "resolved": False,
        "target": None,
        "resolution": "missing",
        "candidates": [],
    }


def build_graph(catalog: dict[str, Any]) -> dict[str, Any]:
    pages = catalog["pages"]
    resolver_index = build_resolver_index(pages)
    nodes = [
        {
            "id": slug,
            "title": page["title"],
            "type": page.get("type"),
            "path": page["path"],
        }
        for slug, page in sorted(pages.items())
    ]
    edges: list[dict[str, Any]] = []
    for slug, page in sorted(pages.items()):
        for target in page["outlinks"]:
            resolution = resolve_link(target, pages, resolver_index)
            edges.append(
                {
                    "source": slug,
                    "raw_target": target,
                    "target": resolution["target"],
                    "resolved": resolution["resolved"],
                    "resolution": resolution["resolution"],
                    "candidates": resolution["candidates"],
                }
            )
    return {"schema_version": 1, "nodes": nodes, "edges": edges}


def run_doctor(
    root: str | Path,
    *,
    ignore_orphans: bool = False,
    require_vault_files: bool = False,
    profile: str | None = None,
) -> DoctorReport:
    from .workflow import REQUIRED_VAULT_FILES

    catalog = build_catalog(root)
    pages = catalog["pages"]
    graph = build_graph(catalog)
    issues: list[dict[str, Any]] = []
    root_path = Path(root).resolve()
    suggestion_cache: dict[str, list[str]] = {}

    if require_vault_files:
        for rel in REQUIRED_VAULT_FILES:
            if not (root_path / rel).exists():
                issues.append(
                    {
                        "code": "WK003",
                        "severity": "error",
                        "path": rel,
                        "message": f"Missing required vault file: {rel}",
                    }
                )

    inbound: dict[str, int] = {slug: 0 for slug in pages}
    for edge in graph["edges"]:
        if edge["resolved"] and edge["target"]:
            inbound[edge["target"]] += 1
            continue

        if edge["resolution"] == "missing":
            suggested_targets = suggestion_cache.setdefault(
                edge["raw_target"],
                _suggest_missing_link_targets(edge["raw_target"], pages),
            )
            issue = {
                "code": "WK001",
                "severity": "error",
                "page": edge["source"],
                "target": edge["raw_target"],
                "message": f"Missing wikilink target: {edge['raw_target']}",
            }
            if suggested_targets:
                issue["suggested_targets"] = suggested_targets
            issues.append(issue)
            continue

        if edge["resolution"] == "ambiguous":
            issues.append(
                {
                    "code": "WK004",
                    "severity": "warning",
                    "page": edge["source"],
                    "target": edge["raw_target"],
                    "candidates": edge["candidates"],
                    "message": f"Ambiguous wikilink target: {edge['raw_target']}",
                }
            )

    if not ignore_orphans and len(pages) > 1:
        for slug, page in sorted(pages.items()):
            if _is_special_page(slug):
                continue
            if inbound[slug] == 0 and not page["outlinks"]:
                issues.append(
                    {
                        "code": "WK002",
                        "severity": "warning",
                        "page": slug,
                        "message": "Page has no inbound links and no outbound links.",
                    }
                )

    if profile == OKF_PROFILE:
        okf_readiness = build_okf_readiness(root_path)
        for item in okf_readiness["missing_type_pages"]:
            issue = {
                "code": "WK010",
                "severity": "warning",
                "path": item["path"],
                "message": "Durable curated page is missing type frontmatter.",
            }
            if item.get("suggested_type"):
                issue["suggested_type"] = item["suggested_type"]
            issues.append(issue)
        for item in okf_readiness["invalid_type_pages"]:
            issues.append(
                {
                    "code": "WK011",
                    "severity": "warning",
                    "path": item["path"],
                    "type": item.get("type"),
                    "message": "Page has an invalid or unknown OKF-compatible type.",
                }
            )
        for item in okf_readiness["malformed_frontmatter_pages"]:
            issues.append(
                {
                    "code": "WK012",
                    "severity": "error",
                    "path": item["path"],
                    "message": "Malformed YAML frontmatter under OKF compatibility profile.",
                }
            )
        for item in okf_readiness["provenance_gap_pages"]:
            issues.append(
                {
                    "code": "WK013",
                    "severity": "warning",
                    "path": item["path"],
                    "message": (
                        "Source-derived page is missing deterministic provenance frontmatter."
                    ),
                }
            )
        for field in okf_readiness["unknown_fields"]:
            # One issue per field rather than per occurrence. The full path
            # list stays available in the okf_readiness inventory; emitting it
            # as issues buries every other finding.
            issues.append(
                {
                    "code": "WK014",
                    "severity": "info",
                    "path": field["paths"][0],
                    "field": field["field"],
                    "count": field["count"],
                    "message": (
                        f"Bespoke frontmatter field is not in the OKF "
                        f"compatibility contract ({field['count']} page(s))."
                    ),
                }
            )
        for item in okf_readiness["export_blocking_shapes"]:
            issues.append(
                {
                    "code": "WK015",
                    "severity": "warning",
                    "path": item["path"],
                    "field": item["field"],
                    "message": "Frontmatter field uses an export-blocking complex value shape.",
                }
            )

    work_queue = _build_work_queue(issues)
    work_queue_groups = _build_work_queue_groups(issues)
    ok = not any(issue["severity"] in {"error", "warning"} for issue in issues)
    return DoctorReport(
        ok=ok,
        exit_code=0 if ok else 1,
        issue_count=len(issues),
        issues=issues,
        work_queue=work_queue,
        work_queue_groups=work_queue_groups,
        summary={
            "pages": catalog["page_count"],
            "edges": len(graph["edges"]),
            "resolved_edges": sum(1 for edge in graph["edges"] if edge["resolved"]),
            "missing_edges": sum(1 for edge in graph["edges"] if edge["resolution"] == "missing"),
            "ambiguous_edges": sum(
                1 for edge in graph["edges"] if edge["resolution"] == "ambiguous"
            ),
            "orphans": sum(1 for issue in issues if issue["code"] == "WK002"),
        },
    )


def build_repair_plan(
    root: str | Path,
    *,
    ignore_orphans: bool = False,
    require_vault_files: bool = False,
    patch_preview: bool = False,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    catalog = build_catalog(root_path)
    report = run_doctor(
        root_path,
        ignore_orphans=ignore_orphans,
        require_vault_files=require_vault_files,
    )
    pages = catalog["pages"]
    operations: list[dict[str, Any]] = []

    for group in report.work_queue_groups:
        if group.get("code") != "WK001":
            continue
        affected_pages = [
            {"slug": slug, "path": pages[slug]["path"]}
            for slug in group.get("pages", [])
            if slug in pages
        ]
        suggested_targets = group.get("suggested_targets", [])
        replacement_target = suggested_targets[0] if len(suggested_targets) == 1 else None
        occurrences = _find_link_occurrences(
            root_path,
            pages,
            group["target"],
            slugs=group.get("pages", []),
            replacement_target=replacement_target,
        )
        operation = {
            "id": f"repair:{group['id']}",
            "issue_code": group["code"],
            "action": "inspect_missing_target",
            "status": "review_required",
            "target": group["target"],
            "affected_pages": affected_pages,
            "occurrences": occurrences,
            "reason": "No single deterministic replacement target was available.",
            "notes": [
                "This plan is advisory only; Wikic did not edit files.",
                "Create the missing page, add an alias, or retarget links manually.",
            ],
        }
        if replacement_target:
            operation["action"] = "retarget_wikilinks"
            operation["replacement_target"] = replacement_target
            operation["reason"] = (
                "Doctor suggested one deterministic existing target for this "
                "missing wikilink group."
            )
            operation["notes"] = [
                "This plan is advisory only; Wikic did not edit files.",
                "Review exact Markdown spelling/anchors/aliases before applying.",
            ]
            if patch_preview:
                operation["patch_preview"] = _build_patch_preview(
                    root_path,
                    occurrences,
                    target=group["target"],
                    replacement_target=replacement_target,
                )
        operations.append(operation)

    retarget_operations = sum(
        1 for operation in operations if operation["action"] == "retarget_wikilinks"
    )
    inspect_operations = sum(
        1 for operation in operations if operation["action"] == "inspect_missing_target"
    )
    return {
        "schema_version": 1,
        "ok": True,
        "root": str(root_path),
        "doctor_exit_code": report.exit_code,
        "summary": {
            "operation_count": len(operations),
            "retarget_operations": retarget_operations,
            "inspect_operations": inspect_operations,
            "review_required": sum(
                1 for operation in operations if operation["status"] == "review_required"
            ),
        },
        "operations": operations,
    }


def _find_link_occurrences(
    root: Path,
    pages: dict[str, dict[str, Any]],
    target: str,
    *,
    slugs: list[str],
    replacement_target: str | None = None,
) -> list[dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    wanted = normalize_slug(target)
    for slug in sorted(slug for slug in slugs if slug in pages):
        rel_path = pages[slug]["path"]
        path = root / rel_path
        text = path.read_text(encoding="utf-8")
        for match in LINK_OCCURRENCE_RE.finditer(text):
            link_target = normalize_slug(match.group(1))
            if link_target != wanted:
                continue
            occurrence = {
                "path": rel_path,
                "slug": slug,
                "line": text.count("\n", 0, match.start()) + 1,
                "raw": match.group(0),
                "target": wanted,
                "status": "review_required",
            }
            if replacement_target:
                occurrence["suggested_replacement"] = f"[[{replacement_target}{match.group(2)}]]"
            occurrences.append(occurrence)
    return occurrences


def _build_patch_preview(
    root: Path,
    occurrences: list[dict[str, Any]],
    *,
    target: str,
    replacement_target: str,
) -> str:
    paths = sorted({occurrence["path"] for occurrence in occurrences})
    hunks: list[str] = []
    for rel_path in paths:
        path = root / rel_path
        original = path.read_text(encoding="utf-8")
        updated = _replace_wikilink_target(original, target, replacement_target)
        if original == updated:
            continue
        hunks.extend(
            unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
            )
        )
    return "".join(hunks)


def _replace_wikilink_target(text: str, target: str, replacement_target: str) -> str:
    wanted = normalize_slug(target)

    def replace(match: re.Match[str]) -> str:
        if normalize_slug(match.group(1)) != wanted:
            return match.group(0)
        return f"[[{replacement_target}{match.group(2)}]]"

    return LINK_OCCURRENCE_RE.sub(replace, text)


def _suggest_missing_link_targets(
    target: str,
    pages: dict[str, dict[str, Any]],
    *,
    limit: int = 5,
) -> list[str]:
    target = normalize_slug(target)
    if not target:
        return []

    scored: dict[str, float] = {}
    for slug, page in pages.items():
        labels = {slug, slug.rsplit("/", 1)[-1]}
        labels.update(normalize_slug(str(alias)) for alias in page.get("aliases", []))
        for label in labels:
            if not label:
                continue
            score = SequenceMatcher(None, target, label).ratio()
            if target in label or (len(label) >= 4 and label in target):
                score = max(score, 0.82)
            if score >= 0.72:
                scored[slug] = max(scored.get(slug, 0.0), score)

    return [
        slug
        for slug, _score in sorted(scored.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _work_queue_action(code: str) -> str:
    if code == "WK001":
        return "create_or_retarget_link"
    if code == "WK004":
        return "disambiguate_link"
    if code == "WK002":
        return "link_or_archive_page"
    if code == "WK003":
        return "create_required_file"
    return "inspect_issue"


def _work_queue_item_id(issue: dict[str, Any]) -> str:
    subject = issue.get("page") or issue.get("path") or "vault"
    target = issue.get("target", "")
    return f"{issue['code']}:{subject}:{target}"


def _work_queue_sort_key(issue: dict[str, Any]) -> tuple[int, str, str, str]:
    severity_rank = {"error": 0, "warning": 1, "info": 2}
    return (
        severity_rank.get(issue["severity"], 99),
        issue["code"],
        issue.get("page") or issue.get("path") or "",
        issue.get("target") or "",
    )


def _build_work_queue(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    work_queue: list[dict[str, Any]] = []
    for issue in sorted(issues, key=_work_queue_sort_key):
        item = {
            "id": _work_queue_item_id(issue),
            "code": issue["code"],
            "severity": issue["severity"],
            "action": _work_queue_action(issue["code"]),
        }
        if "page" in issue:
            item["page"] = issue["page"]
        if "path" in issue:
            item["path"] = issue["path"]
        if "target" in issue:
            item["target"] = issue["target"]
        if issue.get("candidates") is not None:
            item["candidates"] = issue["candidates"]
        if issue.get("suggested_targets"):
            item["suggested_targets"] = issue["suggested_targets"]
        if issue.get("suggested_type"):
            item["suggested_type"] = issue["suggested_type"]
        if issue.get("field"):
            item["field"] = issue["field"]
        item["message"] = issue["message"]
        work_queue.append(item)
    return work_queue


def _build_work_queue_groups(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for issue in issues:
        if issue.get("code") != "WK001" or "target" not in issue:
            continue
        grouped.setdefault((issue["code"], issue["target"]), []).append(issue)

    groups: list[dict[str, Any]] = []
    for (code, target), target_issues in grouped.items():
        if len(target_issues) < 2:
            continue
        pages = sorted(
            issue["page"] for issue in target_issues if isinstance(issue.get("page"), str)
        )
        suggested_targets = sorted(
            {
                suggestion
                for issue in target_issues
                for suggestion in issue.get("suggested_targets", [])
            }
        )
        item = {
            "id": f"{code}:{target}",
            "code": code,
            "severity": "error",
            "action": _work_queue_action(code),
            "target": target,
            "count": len(target_issues),
            "pages": pages,
            "message": f"Missing wikilink target appears on {len(target_issues)} pages: {target}",
        }
        if suggested_targets:
            item["suggested_targets"] = suggested_targets
        groups.append(item)

    return sorted(groups, key=lambda item: (-item["count"], item["target"]))


def _is_special_page(slug: str) -> bool:
    return slug in {"index", "home", "readme", "overview"} or slug.endswith("/index")


def _is_naming_policy_exempt(rel_path: str, patterns: list[str]) -> bool:
    """Return true when rel_path matches a configured naming-policy exemption."""
    return any(fnmatch.fnmatchcase(rel_path, pattern) for pattern in patterns)


def _marker_hotspots(
    root_path: Path, included_files: list[Path], pattern: re.Pattern[str]
) -> list[dict[str, Any]]:
    hotspots: list[dict[str, Any]] = []
    for path in included_files:
        text = path.read_text(encoding="utf-8")
        _, body = split_frontmatter(text)
        marker_count = len(pattern.findall(body))
        if marker_count:
            hotspots.append(
                {
                    "path": path.relative_to(root_path).as_posix(),
                    "marker_count": marker_count,
                }
            )
    return sorted(hotspots, key=lambda item: (-item["marker_count"], item["path"]))[:20]


def _index_coverage(pages: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_top_level: dict[str, dict[str, Any]] = {}
    paths = {str(page["path"]) for page in pages.values()}
    for page in pages.values():
        path = str(page["path"])
        if "/" not in path:
            continue
        top, leaf = path.split("/", 1)
        stats = by_top_level.setdefault(top, {"top_level": top, "page_count": 0})
        if leaf.lower() not in {"index.md", "readme.md"}:
            stats["page_count"] += 1

    coverage: list[dict[str, Any]] = []
    for top, stats in by_top_level.items():
        if stats["page_count"] == 0:
            continue
        index_path = None
        for candidate in (f"{top}/index.md", f"{top}/README.md"):
            if candidate in paths:
                index_path = candidate
                break
        coverage.append(
            {
                "top_level": top,
                "page_count": stats["page_count"],
                "has_index": index_path is not None,
                "index_path": index_path,
            }
        )
    return sorted(coverage, key=lambda item: item["top_level"])


def _large_page_pressure(pages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    large_pages = sorted(
        (
            {
                "slug": page["slug"],
                "path": page["path"],
                "word_count": page["word_count"],
            }
            for page in pages.values()
            if page["word_count"] >= LARGE_PAGE_WORD_THRESHOLD
        ),
        key=lambda item: (-item["word_count"], item["path"]),
    )[:20]
    return {
        "threshold_word_count": LARGE_PAGE_WORD_THRESHOLD,
        "page_count": len(large_pages),
        "pages": large_pages,
    }


def _timeline_section_ranges(body: str) -> list[tuple[int, int]]:
    matches = list(TIMELINE_SECTION_RE.finditer(body))
    if not matches:
        return []

    ranges: list[tuple[int, int]] = []
    for index, match in enumerate(matches):
        section_start = match.end()
        section_end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        section = body[section_start:section_end]
        if TIMELINE_ENTRY_BULLET_RE.search(section) or TIMELINE_ENTRY_HEADING_RE.search(section):
            ranges.append((section_start, section_end))
    return ranges


def _has_compatible_timeline(body: str) -> bool:
    if sentinel := TIMELINE_SENTINEL_RE.search(body):
        timeline_body = body[sentinel.end() :]
        if TIMELINE_ENTRY_BULLET_RE.search(timeline_body) or TIMELINE_ENTRY_HEADING_RE.search(
            timeline_body
        ):
            return True

    return bool(_timeline_section_ranges(body))


def _timeline_entry_count(text: str) -> int:
    return len(TIMELINE_DATE_RE.findall(text))


def _timeline_eligible(rel_path: str, page_type: Any) -> bool:
    return rel_path.startswith(TIMELINE_PATH_PREFIXES) or (
        isinstance(page_type, str) and page_type in TIMELINE_ELIGIBLE_TYPES
    )


def _timeline_coverage(root_path: Path, included_files: list[Path]) -> dict[str, Any]:
    eligible_pages = 0
    pages_with_timeline = 0
    pages_with_dates_outside_timeline = 0
    missing_candidates: list[dict[str, Any]] = []

    for path in included_files:
        rel_path = path.relative_to(root_path).as_posix()
        text = path.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(text)
        page_type = frontmatter.get("type")

        if not _timeline_eligible(rel_path, page_type):
            continue

        eligible_pages += 1
        has_timeline = _has_compatible_timeline(body)
        if has_timeline:
            pages_with_timeline += 1
            continue

        date_count = _timeline_entry_count(body)
        if date_count:
            pages_with_dates_outside_timeline += 1
            missing_candidates.append(
                {
                    "path": rel_path,
                    "type": page_type,
                    "date_count": date_count,
                }
            )

    top_missing_candidates = sorted(
        missing_candidates,
        key=lambda item: (-item["date_count"], item["path"]),
    )[:TIMELINE_TOP_MISSING_LIMIT]

    coverage_ratio = 0.0
    if eligible_pages:
        coverage_ratio = round(pages_with_timeline / eligible_pages, 3)

    return {
        "eligible_types": sorted(TIMELINE_ELIGIBLE_TYPES),
        "eligible_path_prefixes": list(TIMELINE_PATH_PREFIXES),
        "eligible_pages": eligible_pages,
        "pages_with_timeline": pages_with_timeline,
        "coverage_ratio": coverage_ratio,
        "pages_with_dates_outside_timeline": pages_with_dates_outside_timeline,
        "top_missing_candidates": top_missing_candidates,
    }


def _generated_report_policy_summary(
    root_path: Path, included_files: list[Path], config: dict[str, Any]
) -> dict[str, Any]:
    rules = config.get("generated_report_policy", {}).get("rules", [])
    rule_summaries: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    rel_paths = [path.relative_to(root_path).as_posix() for path in included_files]

    for rule in rules:
        path_glob = rule["path_glob"]
        rollup = rule.get("rollup")
        require_frontmatter = rule.get("require_frontmatter", True)
        matched = sorted(path for path in rel_paths if fnmatch.fnmatchcase(path, path_glob))
        rollup_links: set[str] = set()
        if rollup:
            rollup_path = root_path / rollup
            if rollup_path.exists():
                _, rollup_body = split_frontmatter(rollup_path.read_text(encoding="utf-8"))
                rollup_links = set(extract_links(rollup_body))

        rule_violation_count = 0
        for rel_path in matched:
            text = (root_path / rel_path).read_text(encoding="utf-8")
            frontmatter, _ = split_frontmatter(text)
            target_slug = normalize_slug(PurePosixPath(rel_path).with_suffix("").as_posix())
            missing_frontmatter = require_frontmatter and not bool(frontmatter)
            missing_rollup_link = bool(rollup) and target_slug not in rollup_links
            if missing_frontmatter or missing_rollup_link:
                rule_violation_count += 1
                violations.append(
                    {
                        "path": rel_path,
                        "path_glob": path_glob,
                        "rollup": rollup,
                        "missing_frontmatter": missing_frontmatter,
                        "missing_rollup_link": missing_rollup_link,
                    }
                )

        rule_summaries.append(
            {
                "path_glob": path_glob,
                "rollup": rollup,
                "require_frontmatter": require_frontmatter,
                "matched_count": len(matched),
                "violation_count": rule_violation_count,
            }
        )

    return {
        "rule_count": len(rules),
        "violation_count": len(violations),
        "rules": rule_summaries,
        "violations": sorted(violations, key=lambda item: item["path"]),
    }


def _okf_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("okf_profile", {})
    if not isinstance(raw, dict):
        raw = {}
    return {
        "allowed_types": set(raw.get("allowed_types", OKF_ALLOWED_TYPES)),
        "allowed_frontmatter_fields": set(
            raw.get("allowed_frontmatter_fields", OKF_ALLOWED_FRONTMATTER_FIELDS)
        ),
        "provenance_fields": set(raw.get("provenance_fields", OKF_PROVENANCE_FIELDS)),
        "curated_prefixes": tuple(raw.get("curated_prefixes", OKF_CURATED_PREFIXES)),
        "excluded_patterns": list(raw.get("excluded_patterns", OKF_EXCLUDED_PATTERNS)),
        "source_derived_prefixes": tuple(
            raw.get("source_derived_prefixes", OKF_SOURCE_DERIVED_PREFIXES)
        ),
        "provenance_expected_types": set(
            raw.get("provenance_expected_types", OKF_PROVENANCE_EXPECTED_TYPES)
        ),
        "type_suggestions": dict(raw.get("type_suggestions", OKF_TYPE_SUGGESTIONS)),
    }


def _frontmatter_audit(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        return {"has_frontmatter": False, "malformed": False, "complex_fields": []}

    end = text.find("\n---\n", 4)
    if end == -1:
        return {"has_frontmatter": True, "malformed": True, "complex_fields": []}

    raw = text[4:end]
    malformed = False
    complex_fields: set[str] = set()
    current_key: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[0].isspace():
            # A block sequence parses cleanly into a list, so it is not
            # export-blocking. Nested maps and folded scalars are.
            if current_key and not stripped.startswith("- "):
                complex_fields.add(current_key)
            continue
        if ":" not in line:
            malformed = True
            continue
        key, value = line.split(":", 1)
        current_key = key.strip()
        value = value.strip()
        if value.startswith("{") or value in {"|", ">"}:
            complex_fields.add(current_key)
    return {
        "has_frontmatter": True,
        "malformed": malformed,
        "complex_fields": sorted(complex_fields),
    }


def _okf_suggest_type(rel_path: str, config: dict[str, Any]) -> str | None:
    path = PurePosixPath(rel_path)
    if path.name.lower() in {"index.md", "readme.md"}:
        return "index"
    parts = path.parts
    suggestions = config["type_suggestions"]
    # Walk the path from the most specific segment inward, so a nested
    # folder such as wiki/intel/github-repos/repos wins over its parents.
    for part in reversed(parts[:-1]):
        if part in suggestions:
            return suggestions[part]
    return None


def _okf_provenance_expected(
    rel_path: str, frontmatter: dict[str, Any], okf_config: dict[str, Any]
) -> bool:
    page_type = frontmatter.get("type")
    if isinstance(page_type, str) and page_type in okf_config["provenance_expected_types"]:
        return True
    if rel_path.startswith(okf_config["source_derived_prefixes"]):
        return True
    markers = {"source_derived", "source_expected", "provenance_expected"}
    return any(frontmatter.get(marker) is True for marker in markers)


def _okf_in_scope(rel_path: str, frontmatter: dict[str, Any], okf_config: dict[str, Any]) -> bool:
    if _matches_any_exclude_pattern(rel_path, okf_config["excluded_patterns"]):
        return False
    if _is_special_page(PurePosixPath(rel_path).with_suffix("").as_posix()):
        return False
    return rel_path.startswith(okf_config["curated_prefixes"]) or _okf_provenance_expected(
        rel_path, frontmatter, okf_config
    )


def build_okf_readiness(root: str | Path) -> dict[str, Any]:
    """Build the non-mutating OKF compatibility profile audit."""
    root_path = Path(root).resolve()
    config = load_config(root_path)
    okf_config = _okf_config(config)
    included_files, _ = markdown_files(root_path, exclude_patterns=config.get("exclude", []))

    missing_type_pages: list[dict[str, Any]] = []
    invalid_type_pages: list[dict[str, Any]] = []
    malformed_frontmatter_pages: list[dict[str, Any]] = []
    provenance_gaps: list[dict[str, Any]] = []
    export_blocking_shapes: list[dict[str, Any]] = []
    unknown_fields: dict[str, list[str]] = {}
    checked_pages = 0

    for path in included_files:
        rel_path = path.relative_to(root_path).as_posix()
        text = path.read_text(encoding="utf-8")
        frontmatter, _ = split_frontmatter(text)
        fm_audit = _frontmatter_audit(text)
        if not _okf_in_scope(rel_path, frontmatter, okf_config):
            continue

        checked_pages += 1
        suggested_type = _okf_suggest_type(rel_path, okf_config)
        page_type = frontmatter.get("type")
        if not page_type:
            item = {"path": rel_path}
            if suggested_type:
                item["suggested_type"] = suggested_type
            missing_type_pages.append(item)
        elif not isinstance(page_type, str) or page_type not in okf_config["allowed_types"]:
            invalid_type_pages.append({"path": rel_path, "type": page_type})

        if fm_audit["malformed"]:
            malformed_frontmatter_pages.append({"path": rel_path})
        for field in fm_audit["complex_fields"]:
            export_blocking_shapes.append({"path": rel_path, "field": field})

        for key in sorted(frontmatter):
            if key not in okf_config["allowed_frontmatter_fields"]:
                unknown_fields.setdefault(key, []).append(rel_path)

        if _okf_provenance_expected(rel_path, frontmatter, okf_config):
            provenance_values = [
                frontmatter.get(field) for field in okf_config["provenance_fields"]
            ]
            has_provenance = any(value not in (None, "", []) for value in provenance_values)
            if not has_provenance:
                item = {"path": rel_path}
                if isinstance(page_type, str):
                    item["type"] = page_type
                provenance_gaps.append(item)

    unknown_field_inventory = [
        {"field": field, "count": len(paths), "paths": sorted(paths)}
        for field, paths in sorted(unknown_fields.items())
    ]

    return {
        "profile": OKF_PROFILE,
        "total_checked_pages": checked_pages,
        "pages_missing_type": len(missing_type_pages),
        "invalid_type_count": len(invalid_type_pages),
        "provenance_gaps": len(provenance_gaps),
        "unknown_field_count": sum(item["count"] for item in unknown_field_inventory),
        "malformed_frontmatter_count": len(malformed_frontmatter_pages),
        "export_blocking_shape_count": len(export_blocking_shapes),
        "missing_type_pages": sorted(missing_type_pages, key=lambda item: item["path"]),
        "invalid_type_pages": sorted(invalid_type_pages, key=lambda item: item["path"]),
        "provenance_gap_pages": sorted(provenance_gaps, key=lambda item: item["path"]),
        "unknown_fields": unknown_field_inventory,
        "malformed_frontmatter_pages": sorted(
            malformed_frontmatter_pages, key=lambda item: item["path"]
        ),
        "export_blocking_shapes": sorted(
            export_blocking_shapes, key=lambda item: (item["path"], item["field"])
        ),
    }


def build_summary(root: str | Path, *, profile: str | None = None) -> dict[str, Any]:
    """Build a compact deterministic vault-health summary for advisor workflows."""
    root_path = Path(root).resolve()
    catalog = build_catalog(root_path)
    graph = build_graph(catalog)
    doctor = run_doctor(root_path)
    pages = catalog["pages"]
    config = load_config(root_path)
    included_files, _ = markdown_files(root_path, exclude_patterns=config.get("exclude", []))
    naming_policy_exemptions = config.get("naming_policy_exemptions", [])

    top_level: dict[str, int] = {}
    frontmatter_by_top_level: dict[str, dict[str, int | float]] = {}
    naming_policy_violations: list[dict[str, str]] = []
    naming_policy_violation_count = 0
    frontmatter_gaps: list[dict[str, str]] = []

    for included_file in included_files:
        rel_path = included_file.relative_to(root_path).as_posix()
        if _is_naming_policy_exempt(rel_path, naming_policy_exemptions):
            continue
        rel_no_suffix = PurePosixPath(rel_path).with_suffix("").as_posix()
        normalized_rel = normalize_slug(rel_no_suffix)
        if rel_no_suffix != normalized_rel:
            naming_policy_violation_count += 1
            if len(naming_policy_violations) < 100:
                naming_policy_violations.append({"path": rel_path, "expected_slug": normalized_rel})

    for page in pages.values():
        path = str(page["path"])
        top = path.split("/", 1)[0] if "/" in path else "."
        top_level[top] = top_level.get(top, 0) + 1

        frontmatter_stats = frontmatter_by_top_level.setdefault(
            top, {"pages": 0, "with_frontmatter": 0}
        )
        frontmatter_stats["pages"] += 1
        if page.get("type") or page.get("status") or page.get("tags"):
            frontmatter_stats["with_frontmatter"] += 1
        else:
            frontmatter_gaps.append({"path": path, "top_level": top})

    for stats in frontmatter_by_top_level.values():
        pages_count = stats["pages"]
        stats["coverage_percent"] = (
            round((stats["with_frontmatter"] / pages_count) * 100, 1) if pages_count else 0
        )

    issue_counts: dict[str, int] = {}
    for issue in doctor.issues:
        issue_counts[issue["code"]] = issue_counts.get(issue["code"], 0) + 1

    payload = {
        "schema_version": 1,
        "root": str(root_path),
        "catalog": catalog["summary"] | {"page_count": catalog["page_count"]},
        "doctor": {
            "ok": doctor.ok,
            "exit_code": doctor.exit_code,
            "issue_count": doctor.issue_count,
            "issue_counts": dict(sorted(issue_counts.items())),
            "summary": doctor.summary,
            "issues": doctor.issues,
        },
        "graph": {
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
            "resolved_edges": sum(1 for edge in graph["edges"] if edge["resolved"]),
            "missing_edges": sum(1 for edge in graph["edges"] if edge["resolution"] == "missing"),
            "ambiguous_edges": sum(
                1 for edge in graph["edges"] if edge["resolution"] == "ambiguous"
            ),
        },
        "top_level_counts": dict(sorted(top_level.items())),
        "frontmatter_by_top_level": dict(sorted(frontmatter_by_top_level.items())),
        "frontmatter_gaps": sorted(frontmatter_gaps, key=lambda item: item["path"]),
        "action_marker_hotspots": _marker_hotspots(root_path, included_files, ACTION_MARKER_RE),
        "stale_marker_hotspots": _marker_hotspots(root_path, included_files, STALE_MARKER_RE),
        "index_coverage": _index_coverage(pages),
        "large_page_pressure": _large_page_pressure(pages),
        "generated_report_policy": _generated_report_policy_summary(
            root_path, included_files, config
        ),
        "timeline_coverage": _timeline_coverage(root_path, included_files),
        "largest_pages": sorted(
            (
                {
                    "slug": page["slug"],
                    "path": page["path"],
                    "word_count": page["word_count"],
                }
                for page in pages.values()
            ),
            key=lambda item: (-item["word_count"], item["path"]),
        )[:20],
        "naming_policy_violation_count": naming_policy_violation_count,
        "naming_policy_violations": sorted(naming_policy_violations, key=lambda item: item["path"]),
    }
    if profile == OKF_PROFILE:
        payload["okf_readiness"] = build_okf_readiness(root_path)
    return payload


def build_llms_text(
    catalog: dict[str, Any],
    *,
    title: str | None = None,
    full: bool = False,
) -> str:
    pages = catalog["pages"]
    title = title or Path(catalog["root"]).name.replace("-", " ").title()
    lines = [
        f"# {title}",
        "",
        "> Generated by Wikic. Deterministic page catalog for AI agents.",
        "",
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for page in pages.values():
        grouped.setdefault(str(page.get("type") or "page"), []).append(page)
    for kind in sorted(grouped):
        lines.extend([f"## {kind.title()}", ""])
        for page in sorted(grouped[kind], key=lambda p: p["slug"]):
            summary = page.get("summary") or f"{page['word_count']} words"
            lines.append(f"- [{page['title']}]({page['path']}) — {kind} — {summary}")
            if full and page["outlinks"]:
                lines.append(f"  - Links: {', '.join(page['outlinks'])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
