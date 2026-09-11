from __future__ import annotations

import fnmatch
import hashlib
import json
import posixpath
import re
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher, unified_diff
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

LINK_RE = re.compile(r"(?<!!)\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
LINK_OCCURRENCE_RE = re.compile(r"(?<!!)\[\[([^\]|#]+)((?:#[^\]|]+)?(?:\|[^\]]+)?)\]\]")
MARKDOWN_LINK_START_RE = re.compile(r"(?<!!)\[[^\]\n]+\]\(")
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
INDENTED_CODE_RE = re.compile(r"^(?: {4}|\t)")
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
TIMELINE_ELIGIBLE_TYPES = {"company", "person", "project", "tool"}
TIMELINE_PATH_PREFIXES: tuple[str, ...] = ()
TIMELINE_TOP_MISSING_LIMIT = 20
LARGE_PAGE_WORD_THRESHOLD = 1000
REQUIRED_ROOT_FILES: list[str] = []
IGNORED_DIRS = {".git", ".hg", ".svn", ".wikic", "node_modules", ".venv", "venv"}
DEFAULT_EXCLUDE_PATTERNS = [
    "archive/**",
    "archives/**",
    "raw/**",
    "generated/**",
    "tmp/**",
]
OKF_PROFILE = "okf-v0.2"
OKF_SPEC_URL = (
    "https://github.com/GoogleCloudPlatform/open-knowledge-format/"
    "blob/0b87c52c6ef999286c745e19998fdfcd03d5dbee/SPEC.md"
)
VAULT_POLICY_PROFILE = "vault-policy"


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
        "exclude_mode": "extend",
        "exclude": list(DEFAULT_EXCLUDE_PATTERNS),
        "required_root_files": list(REQUIRED_ROOT_FILES),
        "timeline_policy": {
            "eligible_types": sorted(TIMELINE_ELIGIBLE_TYPES),
            "eligible_path_prefixes": list(TIMELINE_PATH_PREFIXES),
        },
        "large_page_word_threshold": LARGE_PAGE_WORD_THRESHOLD,
        "naming_policy_exemptions": [],
        "naming_policy_enabled": True,
        "generated_report_policy": {"rules": []},
        "okf": {"enabled": False, "version": "0.2", "exclude": []},
        "vault_policy": {},
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
    exclude_mode = raw.get("exclude_mode", "extend")
    if not isinstance(exclude_mode, str) or exclude_mode not in {"extend", "replace"}:
        raise ValueError(
            f"Invalid config value for 'exclude_mode' in {config_path}: expected extend or replace"
        )
    merged["exclude_mode"] = exclude_mode
    if "exclude" in raw:
        user_excludes = raw["exclude"]
        user_excludes = _validate_string_list(user_excludes, "exclude", config_path)
        merged["exclude"] = _effective_excludes(
            default_config["exclude"], user_excludes, exclude_mode
        )
    elif exclude_mode == "replace":
        merged["exclude"] = []

    if "required_root_files" in raw:
        required = _validate_string_list(
            raw["required_root_files"], "required_root_files", config_path
        )
        for item in required:
            path = PurePosixPath(item)
            if (
                path.is_absolute()
                or PureWindowsPath(item).is_absolute()
                or bool(PureWindowsPath(item).drive)
                or not item
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise ValueError(
                    "Invalid config value for 'required_root_files' "
                    f"in {config_path}: paths must be safe and relative"
                )
        merged["required_root_files"] = required

    if "timeline_policy" in raw:
        merged["timeline_policy"] = _validate_timeline_policy(raw["timeline_policy"], config_path)

    if "large_page_word_threshold" in raw:
        threshold = raw["large_page_word_threshold"]
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
            raise ValueError(
                "Invalid config value for 'large_page_word_threshold' "
                f"in {config_path}: expected positive integer"
            )
        merged["large_page_word_threshold"] = threshold

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

    if "naming_policy_enabled" in raw:
        if not isinstance(raw["naming_policy_enabled"], bool):
            raise ValueError(
                "Invalid config value for 'naming_policy_enabled' "
                f"in {config_path}: expected boolean"
            )
        merged["naming_policy_enabled"] = raw["naming_policy_enabled"]

    if "generated_report_policy" in raw:
        merged["generated_report_policy"] = _validate_generated_report_policy(
            raw["generated_report_policy"], config_path
        )

    if "okf" in raw:
        merged["okf"] = _validate_okf_config(raw["okf"], config_path)

    if "vault_policy" in raw:
        merged["vault_policy"] = _validate_vault_policy(raw["vault_policy"], config_path)

    for key, value in raw.items():
        if key not in {
            "exclude_mode",
            "exclude",
            "required_root_files",
            "timeline_policy",
            "large_page_word_threshold",
            "naming_policy_exemptions",
            "naming_policy_enabled",
            "generated_report_policy",
            "okf",
            "vault_policy",
        }:
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


def _validate_okf_config(value: Any, config_path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid config value for 'okf' in {config_path}: expected object")
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(
            f"Invalid config value for 'okf.enabled' in {config_path}: expected boolean"
        )
    version = value.get("version", "0.2")
    if version != "0.2":
        raise ValueError(
            f"Invalid config value for 'okf.version' in {config_path}: only 0.2 is supported"
        )
    exclude = _validate_string_list(value.get("exclude", []), "okf.exclude", config_path)
    return {"enabled": enabled, "version": version, "exclude": exclude}


def _validate_string_list(value: Any, key: str, config_path: Path) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(
            f"Invalid config value for '{key}' in {config_path}: expected list of strings"
        )
    return list(value)


def _validate_timeline_policy(value: Any, config_path: Path) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise ValueError(
            f"Invalid config value for 'timeline_policy' in {config_path}: expected object"
        )
    return {
        "eligible_types": _validate_string_list(
            value.get("eligible_types", []), "timeline_policy.eligible_types", config_path
        ),
        "eligible_path_prefixes": _validate_string_list(
            value.get("eligible_path_prefixes", []),
            "timeline_policy.eligible_path_prefixes",
            config_path,
        ),
    }


def _validate_vault_policy(value: Any, config_path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(
            f"Invalid config value for 'vault_policy' in {config_path}: expected object"
        )
    list_keys = (
        "allowed_types",
        "allowed_frontmatter_fields",
        "provenance_fields",
        "provenance_trigger_fields",
        "curated_prefixes",
        "excluded_patterns",
        "source_derived_prefixes",
        "provenance_expected_types",
    )
    normalized: dict[str, Any] = {
        key: _validate_string_list(value.get(key, []), f"vault_policy.{key}", config_path)
        for key in list_keys
    }
    suggestions = value.get("type_suggestions", {})
    if not isinstance(suggestions, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in suggestions.items()
    ):
        raise ValueError(
            "Invalid config value for 'vault_policy.type_suggestions' "
            f"in {config_path}: expected string-to-string object"
        )
    normalized["type_suggestions"] = dict(suggestions)
    return normalized


def _vault_policy_is_enabled(config: dict[str, Any]) -> bool:
    return any(bool(value) for value in config["vault_policy"].values())


def _effective_excludes(
    default_excludes: list[str], user_excludes: list[str], mode: str
) -> list[str]:
    merged = []
    seen: set[str] = set()
    patterns = user_excludes if mode == "replace" else default_excludes + user_excludes
    for pattern in patterns:
        if pattern in seen:
            continue
        seen.add(pattern)
        merged.append(pattern)
    return merged


def normalize_slug(value: str) -> str:
    """Normalize a page reference to an Obsidian-friendly, slash-preserving slug."""
    value = value.strip().replace("\\", "/")
    if value.casefold().endswith(".md"):
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

    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() != ".md":
            continue
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


def _mask_markdown_nonlinks(body: str) -> str:
    chars = list(body)

    def blank(start: int, end: int) -> None:
        for offset in range(start, end):
            if chars[offset] != "\n":
                chars[offset] = " "

    fence: tuple[str, int] | None = None
    cursor = 0
    while cursor < len(body):
        if body[cursor] == "\n":
            cursor += 1
            continue
        line_start = cursor == 0 or body[cursor - 1] == "\n"
        if fence is not None and line_start:
            line_end = body.find("\n", cursor)
            if line_end == -1:
                line_end = len(body)
            visible = body[cursor:line_end].rstrip("\r")
            marker, width = fence
            blank(cursor, line_end)
            if re.fullmatch(rf" {{0,3}}{re.escape(marker)}{{{width},}}[ \t]*", visible):
                fence = None
            cursor = line_end
            continue

        if fence is None and line_start:
            line_end = body.find("\n", cursor)
            if line_end == -1:
                line_end = len(body)
            visible = body[cursor:line_end].rstrip("\r")
            opener = FENCE_OPEN_RE.match(visible)
            if opener is not None:
                run = opener.group(1)
                fence = (run[0], len(run))
                blank(cursor, line_end)
                cursor = line_end
                continue
            if INDENTED_CODE_RE.match(visible):
                blank(cursor, line_end)
                cursor = line_end
                continue

        if fence is None and body.startswith("<!--", cursor):
            closing = body.find("-->", cursor + 4)
            end = len(body) if closing == -1 else closing + 3
            blank(cursor, end)
            cursor = end
            continue

        if fence is None and body[cursor] == "`":
            width = 1
            while cursor + width < len(body) and body[cursor + width] == "`":
                width += 1
            delimiter = "`" * width
            closing = body.find(delimiter, cursor + width)
            while closing != -1 and (
                (closing > 0 and body[closing - 1] == "`")
                or (closing + width < len(body) and body[closing + width] == "`")
            ):
                closing = body.find(delimiter, closing + width)
            if closing != -1:
                blank(cursor, closing + width)
                cursor = closing + width
                continue
            cursor += width
            continue

        cursor += 1
    return "".join(chars)


def _markdown_link_destinations(body: str) -> list[str]:
    body = _mask_markdown_nonlinks(body)
    destinations: list[str] = []
    for match in MARKDOWN_LINK_START_RE.finditer(body):
        cursor = match.end()
        while cursor < len(body) and body[cursor].isspace():
            cursor += 1
        if cursor >= len(body):
            continue
        if body[cursor] == "<":
            end = body.find(">", cursor + 1)
            if end == -1:
                continue
            destination = body[cursor + 1 : end]
            cursor = end + 1
        else:
            start = cursor
            depth = 0
            escaped = False
            while cursor < len(body):
                char = body[cursor]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "(":
                    depth += 1
                elif char == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif char.isspace() and depth == 0:
                    break
                cursor += 1
            destination = body[start:cursor]
        if not destination:
            continue
        while cursor < len(body) and body[cursor].isspace():
            cursor += 1
        if cursor >= len(body):
            continue
        if body[cursor] != ")":
            opener = body[cursor]
            closer = {'"': '"', "'": "'", "(": ")"}.get(opener)
            if closer is None:
                continue
            cursor += 1
            while cursor < len(body) and body[cursor] != closer:
                cursor += 1
            if cursor >= len(body):
                continue
            cursor += 1
            while cursor < len(body) and body[cursor].isspace():
                cursor += 1
            if cursor >= len(body) or body[cursor] != ")":
                continue
        destinations.append(destination)
    return destinations


def _markdown_destination(
    destination: str, source_path: str | None
) -> tuple[str | None, str | None]:
    destination = destination.strip()
    destination = re.sub(r"\\([!\"#$%&'()*+,./:;<=>?@\[\]^_`{|}~-])", r"\1", destination)
    if not destination or destination.startswith("#"):
        return None, None
    parsed = urlsplit(destination)
    if parsed.scheme or parsed.netloc:
        return None, None
    path = unquote(parsed.path)
    if "\\" in path:
        return None, "invalid_path_separator"
    if not path or path.startswith("/"):
        return None, None
    if path.endswith("/"):
        path += "index.md"
    if PurePosixPath(path).suffix and not path.casefold().endswith(".md"):
        return None, None
    if path.casefold().endswith(".md"):
        path = path[:-3]
    target = path
    if source_path is not None:
        target = posixpath.normpath(posixpath.join(posixpath.dirname(source_path), path))
    if target == ".." or target.startswith("../"):
        return None, "outside_vault"
    return normalize_slug(target), None


def _extract_links_and_invalid(
    body: str, source_path: str | None = None
) -> tuple[list[str], list[dict[str, str]]]:
    seen: set[str] = set()
    links: list[str] = []
    invalid: list[dict[str, str]] = []
    masked_body = _mask_markdown_nonlinks(body)
    for match in LINK_RE.finditer(masked_body):
        slug = normalize_slug(match.group(1))
        if slug and slug not in seen:
            links.append(slug)
            seen.add(slug)
    for destination in _markdown_link_destinations(masked_body):
        slug, reason = _markdown_destination(destination, source_path)
        if reason is not None:
            invalid.append({"target": destination, "reason": reason})
        elif slug and slug not in seen:
            links.append(slug)
            seen.add(slug)
    return links, invalid


def extract_links(body: str, source_path: str | None = None) -> list[str]:
    links, _ = _extract_links_and_invalid(body, source_path)
    return links


def build_catalog(root: str | Path) -> dict[str, Any]:
    root_path = Path(root).resolve()
    config = load_config(root_path)
    pages_files, excluded_count = markdown_files(
        root_path, exclude_patterns=config.get("exclude", [])
    )
    pages: dict[str, dict[str, Any]] = {}
    for path in pages_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        frontmatter, body = split_frontmatter(text)
        aliases = frontmatter.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        elif not isinstance(aliases, list):
            aliases = []
        rel = path.relative_to(root_path).with_suffix("").as_posix()
        slug = normalize_slug(rel)
        outlinks, invalid_outlinks = _extract_links_and_invalid(body, rel)
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
            "outlinks": outlinks,
            "invalid_outlinks": invalid_outlinks,
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
        for invalid in page.get("invalid_outlinks", []):
            edges.append(
                {
                    "source": slug,
                    "raw_target": invalid["target"],
                    "target": None,
                    "resolved": False,
                    "resolution": invalid["reason"],
                    "candidates": [],
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
    catalog = build_catalog(root)
    pages = catalog["pages"]
    graph = build_graph(catalog)
    issues: list[dict[str, Any]] = []
    root_path = Path(root).resolve()
    config = load_config(root_path)
    suggestion_cache: dict[str, list[str]] = {}

    if require_vault_files:
        for rel in config["required_root_files"]:
            candidate = root_path / rel
            try:
                candidate.resolve().relative_to(root_path)
                safe_file = candidate.is_file()
            except ValueError:
                safe_file = False
            if not safe_file:
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
                "message": f"Missing internal link target: {edge['raw_target']}",
            }
            if suggested_targets:
                issue["suggested_targets"] = suggested_targets
            issues.append(issue)
            continue

        if edge["resolution"] in {"outside_vault", "invalid_path_separator"}:
            reason = edge["resolution"]
            message = (
                "Relative Markdown link escapes the vault root."
                if reason == "outside_vault"
                else "Relative Markdown link contains an invalid backslash path separator."
            )
            issues.append(
                {
                    "code": "WK005",
                    "severity": "error",
                    "page": edge["source"],
                    "target": edge["raw_target"],
                    "reason": reason,
                    "message": message,
                }
            )
            continue

        if edge["resolution"] == "ambiguous":
            issues.append(
                {
                    "code": "WK004",
                    "severity": "warning",
                    "page": edge["source"],
                    "target": edge["raw_target"],
                    "candidates": edge["candidates"],
                    "message": f"Ambiguous internal link target: {edge['raw_target']}",
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

    if config["okf"]["enabled"] or profile == OKF_PROFILE:
        okf_readiness = build_okf_readiness(root_path)
        for item in okf_readiness["missing_frontmatter_pages"]:
            issues.append(
                {
                    "code": "WK010",
                    "severity": "error",
                    "path": item["path"],
                    "message": "OKF concept document is missing YAML frontmatter.",
                }
            )
        for item in okf_readiness["invalid_type_pages"]:
            issues.append(
                {
                    "code": "WK011",
                    "severity": "error",
                    "path": item["path"],
                    "type": item.get("type"),
                    "message": "OKF concept type must be a non-empty string.",
                }
            )
        for item in okf_readiness["malformed_frontmatter_pages"]:
            issues.append(
                {
                    "code": "WK012",
                    "severity": "error",
                    "path": item["path"],
                    "message": "OKF concept has malformed YAML frontmatter.",
                }
            )
        for item in okf_readiness["invalid_index_files"]:
            issues.append(
                {
                    "code": "WK013",
                    "severity": "error",
                    **item,
                    "message": "Reserved index.md does not follow OKF v0.2 structure.",
                }
            )
        for item in okf_readiness["invalid_log_files"]:
            issues.append(
                {
                    "code": "WK014",
                    "severity": "error",
                    **item,
                    "message": "Reserved log.md does not follow OKF v0.2 structure.",
                }
            )
        for item in okf_readiness["invalid_utf8_files"]:
            issues.append(
                {
                    "code": "WK015",
                    "severity": "error",
                    "path": item["path"],
                    "message": "OKF Markdown file is not valid UTF-8.",
                }
            )

    if _vault_policy_is_enabled(config) or profile == VAULT_POLICY_PROFILE:
        policy_readiness = build_vault_policy_readiness(root_path)
        for item in policy_readiness["missing_type_pages"]:
            issue: dict[str, Any] = {
                "code": "WK020",
                "severity": "warning",
                "path": item["path"],
                "message": "Page selected by vault policy is missing type frontmatter.",
            }
            if item.get("suggested_type"):
                issue["suggested_type"] = item["suggested_type"]
            issues.append(issue)
        for item in policy_readiness["invalid_type_pages"]:
            issues.append(
                {
                    "code": "WK021",
                    "severity": "warning",
                    "path": item["path"],
                    "type": item.get("type"),
                    "message": "Page type is not allowed by the configured vault policy.",
                }
            )
        for item in policy_readiness["malformed_frontmatter_pages"]:
            issues.append(
                {
                    "code": "WK022",
                    "severity": "error",
                    "path": item["path"],
                    "message": "Malformed frontmatter under configured vault policy.",
                }
            )
        for item in policy_readiness["provenance_gap_pages"]:
            issues.append(
                {
                    "code": "WK023",
                    "severity": "warning",
                    "path": item["path"],
                    "message": "Page is missing provenance required by configured vault policy.",
                }
            )
        for field in policy_readiness["unknown_fields"]:
            issues.append(
                {
                    "code": "WK024",
                    "severity": "info",
                    "path": field["paths"][0],
                    "field": field["field"],
                    "count": field["count"],
                    "message": (
                        f"Frontmatter field is outside configured vault policy "
                        f"({field['count']} page(s))."
                    ),
                }
            )
        for item in policy_readiness["export_blocking_shapes"]:
            issues.append(
                {
                    "code": "WK025",
                    "severity": "warning",
                    "path": item["path"],
                    "field": item["field"],
                    "message": "Frontmatter shape is disallowed by configured vault policy.",
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
            "invalid_edges": sum(
                1
                for edge in graph["edges"]
                if edge["resolution"] in {"outside_vault", "invalid_path_separator"}
            ),
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
        if replacement_target and occurrences:
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
        elif replacement_target:
            operation["reason"] = (
                "Doctor suggested one existing target, but the source uses a link "
                "syntax that Wikic does not rewrite automatically."
            )
            operation["notes"] = [
                "This plan is advisory only; Wikic did not edit files.",
                (
                    "Retarget the relative Markdown link manually and preserve its "
                    "source-relative path."
                ),
            ]
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
            "message": (
                f"Missing internal link target appears on {len(target_issues)} pages: {target}"
            ),
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
        text = path.read_text(encoding="utf-8", errors="replace")
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


def _large_page_pressure(pages: dict[str, dict[str, Any]], threshold: int) -> dict[str, Any]:
    large_pages = sorted(
        (
            {
                "slug": page["slug"],
                "path": page["path"],
                "word_count": page["word_count"],
            }
            for page in pages.values()
            if page["word_count"] >= threshold
        ),
        key=lambda item: (-item["word_count"], item["path"]),
    )[:20]
    return {
        "threshold_word_count": threshold,
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


def _timeline_eligible(
    rel_path: str, page_type: Any, eligible_types: set[str], eligible_prefixes: tuple[str, ...]
) -> bool:
    return rel_path.startswith(eligible_prefixes) or (
        isinstance(page_type, str) and page_type in eligible_types
    )


def _timeline_coverage(
    root_path: Path, included_files: list[Path], policy: dict[str, list[str]]
) -> dict[str, Any]:
    eligible_types = set(policy["eligible_types"])
    eligible_prefixes = tuple(policy["eligible_path_prefixes"])
    eligible_pages = 0
    pages_with_timeline = 0
    pages_with_dates_outside_timeline = 0
    missing_candidates: list[dict[str, Any]] = []

    for path in included_files:
        rel_path = path.relative_to(root_path).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        frontmatter, body = split_frontmatter(text)
        page_type = frontmatter.get("type")

        if not _timeline_eligible(rel_path, page_type, eligible_types, eligible_prefixes):
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
        "eligible_types": sorted(eligible_types),
        "eligible_path_prefixes": list(eligible_prefixes),
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
                _, rollup_body = split_frontmatter(
                    rollup_path.read_text(encoding="utf-8", errors="replace")
                )
                rollup_links = set(extract_links(rollup_body))

        rule_violation_count = 0
        for rel_path in matched:
            text = (root_path / rel_path).read_text(encoding="utf-8", errors="replace")
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


def _vault_policy_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("vault_policy", {})
    if not isinstance(raw, dict):
        raw = {}
    return {
        "allowed_types": set(raw.get("allowed_types", [])),
        "allowed_frontmatter_fields": set(raw.get("allowed_frontmatter_fields", [])),
        "provenance_fields": set(raw.get("provenance_fields", [])),
        "provenance_trigger_fields": tuple(raw.get("provenance_trigger_fields", [])),
        "curated_prefixes": tuple(raw.get("curated_prefixes", [])),
        "excluded_patterns": list(raw.get("excluded_patterns", [])),
        "source_derived_prefixes": tuple(raw.get("source_derived_prefixes", [])),
        "provenance_expected_types": set(raw.get("provenance_expected_types", [])),
        "type_suggestions": dict(raw.get("type_suggestions", {})),
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


def _vault_policy_suggest_type(rel_path: str, config: dict[str, Any]) -> str | None:
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


def _vault_policy_provenance_expected(
    rel_path: str, frontmatter: dict[str, Any], policy: dict[str, Any]
) -> bool:
    page_type = frontmatter.get("type")
    if isinstance(page_type, str) and page_type in policy["provenance_expected_types"]:
        return True
    if rel_path.startswith(policy["source_derived_prefixes"]):
        return True
    return any(frontmatter.get(field) is True for field in policy["provenance_trigger_fields"])


def _vault_policy_in_scope(
    rel_path: str, frontmatter: dict[str, Any], policy: dict[str, Any]
) -> bool:
    if _matches_any_exclude_pattern(rel_path, policy["excluded_patterns"]):
        return False
    if _is_special_page(PurePosixPath(rel_path).with_suffix("").as_posix()):
        return False
    return rel_path.startswith(policy["curated_prefixes"]) or _vault_policy_provenance_expected(
        rel_path, frontmatter, policy
    )


def build_vault_policy_readiness(root: str | Path) -> dict[str, Any]:
    """Audit optional producer-defined vault policy without claiming standard conformance."""
    root_path = Path(root).resolve()
    config = load_config(root_path)
    policy = _vault_policy_config(config)
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
        text = path.read_text(encoding="utf-8", errors="replace")
        frontmatter, _ = split_frontmatter(text)
        fm_audit = _frontmatter_audit(text)
        if not _vault_policy_in_scope(rel_path, frontmatter, policy):
            continue

        checked_pages += 1
        suggested_type = _vault_policy_suggest_type(rel_path, policy)
        page_type = frontmatter.get("type")
        if not page_type:
            item = {"path": rel_path}
            if suggested_type:
                item["suggested_type"] = suggested_type
            missing_type_pages.append(item)
        elif policy["allowed_types"] and (
            not isinstance(page_type, str) or page_type not in policy["allowed_types"]
        ):
            invalid_type_pages.append({"path": rel_path, "type": page_type})

        if fm_audit["malformed"]:
            malformed_frontmatter_pages.append({"path": rel_path})
        for field in fm_audit["complex_fields"]:
            export_blocking_shapes.append({"path": rel_path, "field": field})

        for key in sorted(frontmatter):
            if (
                policy["allowed_frontmatter_fields"]
                and key not in policy["allowed_frontmatter_fields"]
            ):
                unknown_fields.setdefault(key, []).append(rel_path)

        if _vault_policy_provenance_expected(rel_path, frontmatter, policy):
            provenance_values = [frontmatter.get(field) for field in policy["provenance_fields"]]
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
        "profile": VAULT_POLICY_PROFILE,
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


def _parse_yaml_frontmatter(text: str) -> tuple[bool, bool, dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return False, False, {}, text
    try:
        end = lines.index("---", 1)
    except ValueError:
        return True, True, {}, ""
    try:
        parsed = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        return True, True, {}, "\n".join(lines[end + 1 :])
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict) or any(not isinstance(key, str) for key in parsed):
        return True, True, {}, "\n".join(lines[end + 1 :])
    return True, False, parsed, "\n".join(lines[end + 1 :])


def _okf_markdown_files(root_path: Path) -> list[Path]:
    files = []
    for path in root_path.rglob("*"):
        if not path.is_file() or path.suffix.casefold() != ".md":
            continue
        try:
            path.resolve().relative_to(root_path)
        except (OSError, ValueError):
            continue
        if path.is_file():
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root_path).as_posix())


def _okf_index_issue(rel_path: str, text: str, root_index: bool) -> dict[str, Any] | None:
    has_frontmatter, malformed, frontmatter, body = _parse_yaml_frontmatter(text)
    if malformed:
        return {"path": rel_path, "reason": "malformed_frontmatter"}
    if has_frontmatter:
        if not root_index:
            return {"path": rel_path, "reason": "frontmatter_not_allowed"}
        if set(frontmatter) != {"okf_version"}:
            return {"path": rel_path, "reason": "unsupported_root_frontmatter"}
        version = frontmatter["okf_version"]
        if not isinstance(version, str) or version != "0.2":
            return {"path": rel_path, "reason": "wrong_okf_version"}
    headings = list(re.finditer(r"(?m)^(#{1,6})\s+\S.*$", body))
    if not headings:
        return {"path": rel_path, "reason": "missing_section_heading"}
    source_path = PurePosixPath(rel_path).with_suffix("").as_posix()
    link_slugs = [
        slug
        for destination in _markdown_link_destinations(body)
        if (slug := _markdown_destination(destination, source_path)[0]) is not None
    ]
    if not link_slugs:
        return {"path": rel_path, "reason": "missing_markdown_link_entry"}
    return None


def _okf_log_issue(rel_path: str, text: str) -> dict[str, Any] | None:
    _, malformed, _, body = _parse_yaml_frontmatter(text)
    if malformed:
        return {"path": rel_path, "reason": "malformed_frontmatter"}
    lines = body.splitlines()
    nonblank = [index for index, line in enumerate(lines) if line.strip()]
    if not nonblank or re.fullmatch(r"#\s+\S.*", lines[nonblank[0]]) is None:
        return {"path": rel_path, "reason": "missing_title"}
    headings = re.findall(r"(?m)^##\s+(.+?)\s*$", body)
    if not headings:
        return {"path": rel_path, "reason": "invalid_date_heading"}
    try:
        parsed_dates = [date.fromisoformat(heading) for heading in headings]
    except ValueError:
        return {"path": rel_path, "reason": "invalid_date_heading"}
    if parsed_dates != sorted(parsed_dates, reverse=True):
        return {"path": rel_path, "reason": "dates_not_newest_first"}
    first_date = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(r"##\s+\d{4}-\d{2}-\d{2}\s*", line)
        ),
        None,
    )
    if first_date is None or any(line.strip() for line in lines[nonblank[0] + 1 : first_date]):
        return {"path": rel_path, "reason": "content_outside_date_section"}
    sections = re.split(r"(?m)^##\s+\d{4}-\d{2}-\d{2}\s*$", body)[1:]
    bullet = re.compile(r"^\s*[*+-]\s+\S")
    for section in sections:
        entries = [line for line in section.splitlines() if line.strip()]
        if not entries or bullet.match(entries[0]) is None:
            return {"path": rel_path, "reason": "invalid_date_entries"}
        if any(
            bullet.match(line) is None and not line.startswith(("  ", "\t")) for line in entries[1:]
        ):
            return {"path": rel_path, "reason": "invalid_date_entries"}
    return None


def build_okf_readiness(root: str | Path) -> dict[str, Any]:
    """Validate the normative conformance rules in the public OKF v0.2 specification."""
    root_path = Path(root).resolve()
    config = load_config(root_path)
    exclude_patterns = config["okf"]["exclude"]
    missing_frontmatter: list[dict[str, str]] = []
    malformed_frontmatter: list[dict[str, str]] = []
    invalid_type: list[dict[str, Any]] = []
    invalid_indexes: list[dict[str, str]] = []
    invalid_logs: list[dict[str, str]] = []
    invalid_utf8: list[dict[str, str]] = []
    concept_count = 0
    reserved_count = 0

    all_markdown_files = _okf_markdown_files(root_path)
    markdown_files = [
        path
        for path in all_markdown_files
        if not _matches_any_exclude_pattern(
            path.relative_to(root_path).as_posix(), exclude_patterns
        )
    ]
    for path in markdown_files:
        rel_path = path.relative_to(root_path).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            invalid_utf8.append({"path": rel_path})
            continue
        name = path.name
        if name == "index.md":
            reserved_count += 1
            if issue := _okf_index_issue(rel_path, text, path.parent == root_path):
                invalid_indexes.append(issue)
            continue
        if name == "log.md":
            reserved_count += 1
            if issue := _okf_log_issue(rel_path, text):
                invalid_logs.append(issue)
            continue

        concept_count += 1
        has_frontmatter, malformed, frontmatter, _ = _parse_yaml_frontmatter(text)
        if not has_frontmatter:
            missing_frontmatter.append({"path": rel_path})
            continue
        if malformed:
            malformed_frontmatter.append({"path": rel_path})
            continue
        page_type = frontmatter.get("type")
        if not isinstance(page_type, str) or not page_type.strip():
            invalid_type.append({"path": rel_path, "type": repr(page_type)[:200]})

    conformant = not any(
        (
            missing_frontmatter,
            malformed_frontmatter,
            invalid_type,
            invalid_indexes,
            invalid_logs,
            invalid_utf8,
        )
    )
    return {
        "profile": OKF_PROFILE,
        "okf_version": "0.2",
        "specification": OKF_SPEC_URL,
        "exclude_patterns": exclude_patterns,
        "excluded_markdown_count": len(all_markdown_files) - len(markdown_files),
        "conformant": conformant,
        "concept_document_count": concept_count,
        "reserved_file_count": reserved_count,
        "missing_frontmatter_count": len(missing_frontmatter),
        "malformed_frontmatter_count": len(malformed_frontmatter),
        "invalid_type_count": len(invalid_type),
        "invalid_index_count": len(invalid_indexes),
        "invalid_log_count": len(invalid_logs),
        "invalid_utf8_count": len(invalid_utf8),
        "missing_frontmatter_pages": missing_frontmatter,
        "malformed_frontmatter_pages": malformed_frontmatter,
        "invalid_type_pages": invalid_type,
        "invalid_index_files": invalid_indexes,
        "invalid_log_files": invalid_logs,
        "invalid_utf8_files": invalid_utf8,
    }


def build_summary(root: str | Path, *, profile: str | None = None) -> dict[str, Any]:
    """Build a compact deterministic vault-health summary for advisor workflows."""
    root_path = Path(root).resolve()
    catalog = build_catalog(root_path)
    graph = build_graph(catalog)
    doctor = run_doctor(root_path, profile=profile)
    pages = catalog["pages"]
    config = load_config(root_path)
    included_files, _ = markdown_files(root_path, exclude_patterns=config.get("exclude", []))
    naming_policy_exemptions = config.get("naming_policy_exemptions", [])

    top_level: dict[str, int] = {}
    frontmatter_by_top_level: dict[str, dict[str, int | float]] = {}
    naming_policy_violations: list[dict[str, str]] = []
    naming_policy_violation_count = 0
    frontmatter_gaps: list[dict[str, str]] = []

    naming_policy_files = included_files if config["naming_policy_enabled"] else []
    for included_file in naming_policy_files:
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
            "invalid_edges": sum(
                1
                for edge in graph["edges"]
                if edge["resolution"] in {"outside_vault", "invalid_path_separator"}
            ),
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
        "large_page_pressure": _large_page_pressure(pages, config["large_page_word_threshold"]),
        "generated_report_policy": _generated_report_policy_summary(
            root_path, included_files, config
        ),
        "timeline_coverage": _timeline_coverage(
            root_path, included_files, config["timeline_policy"]
        ),
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
    if config["okf"]["enabled"] or profile == OKF_PROFILE:
        payload["okf_readiness"] = build_okf_readiness(root_path)
    if _vault_policy_is_enabled(config) or profile == VAULT_POLICY_PROFILE:
        payload["vault_policy_readiness"] = build_vault_policy_readiness(root_path)
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
