# Wikic

Wikic is a deterministic CLI compiler and doctor for agent-readable Markdown / LLM wikis.

It is deliberately **not** a hosted second brain, vector database, or autonomous writer. Its job is to make a Markdown vault easier for humans and AI agents to maintain by compiling structural artifacts and surfacing hygiene issues before retrieval or generation layers touch the content.

## Core idea

```text
Markdown vault -> catalog.json -> graph.json -> llms.txt -> doctor report
              -> source capture -> candidate review -> path-safe promotion
```

- **Compile, don't retrieve:** pre-structure the vault for agent navigation. RAG can index the result later.
- **Deterministic first:** checks that can block commits should not depend on an LLM.
- **Agent-readable outputs:** JSON artifacts and `llms.txt` are built for automation.
- **No hidden mutations:** commands write explicit artifacts or print to stdout.

## Install for development

```bash
uv sync
uv run wikic doctor --root path/to/vault
```

## Commands

```bash
wikic doctor --root . --json
wikic health --root .
wikic catalog --root .
wikic graph --root .
wikic llms --root . --title "My Vault"
wikic summary --root . --json
wikic summary --root . --profile okf-v0.2 --json
wikic summary --root . --profile vault-policy --json
wikic ingest ./source.md --vault . --json
wikic review list --vault . --json
wikic review show CANDIDATE_ID --vault . --json
wikic review accept CANDIDATE_ID --vault . --dry-run --json
wikic review accept CANDIDATE_ID --vault . --apply --json
wikic lint --candidates --vault . --json
```

### `doctor`

Runs deterministic health checks:

- `WK001` — missing wikilink target.
- `WK002` — isolated page with no inbound and no outbound links.
- `WK003` — missing configured root file when `--require-vault-files` is set.

Exit code is `0` when clean, `1` when issues are found.

### `catalog`

Writes `.wikic/catalog.json`, containing every Markdown page, title, frontmatter metadata, wikilinks, word count, and content hash.

### `graph`

Writes `.wikic/graph.json`, containing nodes and resolved/missing edges.

### `llms`

Writes `llms.txt`, a compact agent navigation file grouped by page type.

### `summary`

Prints a compact deterministic advisor snapshot: catalog/doctor/graph stats, top-level counts, frontmatter coverage and exact `frontmatter_gaps`, timeline coverage, action/stale marker hotspots, index coverage, large-page pressure, generated-report policy violations, largest pages, and naming-policy violations.

With `--profile okf-v0.2 --json`, `summary` includes an `okf_readiness` object that validates the normative conformance rules in the public [Open Knowledge Format v0.2 specification](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/0b87c52c6ef999286c745e19998fdfcd03d5dbee/SPEC.md). It inspects every Markdown file under the declared bundle root, including hidden directories: concept documents need valid UTF-8, parseable YAML frontmatter, and a non-empty string `type`; exact lowercase reserved `index.md` and `log.md` files are validated separately. Unknown types, additional keys, nested YAML, missing optional metadata, broken links, and absent indexes are accepted as the specification requires.

Producer-specific taxonomy, provenance, path, and export-shape opinions belong to the separate `vault-policy` profile. This profile has no built-in taxonomy or path scope: it runs only rules supplied by the vault's `.wikic/config.json`. `doctor`/`health` expose OKF diagnostics as WK010-WK015 and configured vault-policy diagnostics as WK020-WK025. The default profile remains Wikic's own structural health check.

### Configuration and defaults

Wikic keeps its configurable product-opinion defaults deliberately small:

- `exclude` extends `archive/**`, `archives/**`, `raw/**`, `generated/**`, and `tmp/**` unless `exclude_mode` is `replace`.
- Timeline coverage applies to `company`, `person`, `project`, and `tool` types, with no path-prefix assumptions.
- Large-page pressure starts at 1,000 words.
- No root files, vault taxonomy, provenance aliases, or custom frontmatter allowlist are required by default.
- Lowercase, hyphenated paths are the default naming convention; set `naming_policy_enabled` to `false` to disable it or add explicit exemptions.

Timeline and vault-policy lists replace their corresponding defaults; an empty list disables that list-driven rule. `exclude` is the exception: it extends defaults unless `exclude_mode` is `replace`. Naming enforcement uses the separate boolean switch because an empty exemption list means no paths are exempt.

Configure a vault in `.wikic/config.json`:

```json
{
  "exclude_mode": "replace",
  "exclude": ["build/**", "drafts/**"],
  "required_root_files": ["index.md", "policy/schema.md"],
  "timeline_policy": {
    "eligible_types": ["project", "service"],
    "eligible_path_prefixes": ["operations/"]
  },
  "large_page_word_threshold": 1500,
  "naming_policy_enabled": true,
  "naming_policy_exemptions": [
    "README.md",
    "catalog/repositories/*--*.md"
  ],
  "generated_report_policy": {
    "rules": [
      {
        "path_glob": "reports/weekly/*.md",
        "rollup": "reports/index.md",
        "require_frontmatter": true
      }
    ]
  },
  "vault_policy": {
    "allowed_types": ["project", "reference"],
    "allowed_frontmatter_fields": ["title", "type", "source_url"],
    "provenance_fields": ["source_url"],
    "provenance_trigger_fields": ["source_expected"],
    "curated_prefixes": ["knowledge/"],
    "excluded_patterns": ["knowledge/drafts/**"],
    "source_derived_prefixes": ["knowledge/sources/"],
    "provenance_expected_types": ["reference"],
    "type_suggestions": {"projects": "project"}
  }
}
```

Generated report policy checks are evidence-only summary fields: Wikic reports whether matching report files have YAML frontmatter and are linked from their configured rollup. Delivery and downstream indexing requirements remain workflow policy outside the deterministic file scan.

### `ingest`

Captures a source file into `.wikic/sources/` and writes a manifest containing the original path, captured path, byte count, and SHA-256.

### `review`

Manages candidate page JSON files in `.wikic/candidates/`:

```json
{
  "id": "example-concept",
  "target_path": "concepts/example.md",
  "content": "# Example\n\nMarkdown to write.\n",
  "source_ids": ["source-id-from-ingest"]
}
```

`review accept` requires exactly one of `--dry-run` or `--apply`. Applied candidates are written to the target path and archived to `.wikic/accepted/` with before/after hashes.

Safety constraints:

- `target_path` must be relative to the vault.
- No absolute paths.
- No `..` escapes.
- Cannot write under any hidden top-level directory (for example `.git/` or `.wikic/`).

### `lint`

`wikic lint --candidates` validates candidate shape and path safety.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the public-by-default security rule, local hooks, and pull-request workflow. Report vulnerabilities using [SECURITY.md](SECURITY.md).

```bash
uv sync --locked --all-groups
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

## License

MIT — see [LICENSE](LICENSE).

## Scope

Wikic is an independent implementation of the LLM-wiki compiler pattern. It reads plain Markdown and YAML frontmatter, so it works against any vault that follows those conventions rather than a single tool's format.

Contributions should stay dependency-light and deterministic. Anything that can block a commit must not depend on a model.
