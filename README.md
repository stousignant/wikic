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
wikic summary --root . --profile okf-compatible --json
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
- `WK003` — missing required vault operating file when `--require-vault-files` is set.

Exit code is `0` when clean, `1` when issues are found.

### `catalog`

Writes `.wikic/catalog.json`, containing every Markdown page, title, frontmatter metadata, wikilinks, word count, and content hash.

### `graph`

Writes `.wikic/graph.json`, containing nodes and resolved/missing edges.

### `llms`

Writes `llms.txt`, a compact agent navigation file grouped by page type.

### `summary`

Prints a compact deterministic advisor snapshot: catalog/doctor/graph stats, top-level counts, frontmatter coverage and exact `frontmatter_gaps`, timeline coverage, action/stale marker hotspots, index coverage, large-page pressure, generated-report policy violations, largest pages, and naming-policy violations.

With `--profile okf-compatible --json`, `summary` also includes an `okf_readiness` object. This is a non-mutating compatibility audit for durable, curated pages that may later be exported or normalized into OKF/OpenWiki dialects. Wikic remains the authority for the local vault's structural health; OKF/OpenWiki are compatibility/input-output dialects only.

The OKF profile is deterministic and config-backed. It checks curated/source-derived pages for missing or unknown `type`, malformed frontmatter, missing deterministic provenance (`source`, `sources`, `source_url`, `source_uri`, or configured equivalents), bespoke frontmatter fields, and export-blocking complex metadata shapes. Type suggestions are deterministic folder/config mappings, not LLM output. Source-layer and scratch material such as `raw/`, `archive/`, generated reports, and scratch/planning folders are excluded from canonical OKF-readiness failures by default.

The same profile can be requested on `doctor`/`health` to emit WK010-WK015 diagnostics. Without `--profile okf-compatible`, default `summary` and `doctor` output shapes and exit-code behavior are unchanged.

Naming and generated-report policy are configured per vault in `.wikic/config.json`:

```json
{
  "naming_policy_exemptions": [
    "README.md",
    "SCHEMA.md",
    "research/github-repos/repos/*--*.md"
  ],
  "generated_report_policy": {
    "rules": [
      {
        "path_glob": "reports/vault-cleanup-advisor/*.md",
        "rollup": "reports/index.md",
        "require_frontmatter": true
      }
    ]
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
