# Wikic Agent Notes

Wikic is a deterministic Markdown/LLM-wiki compiler.

## Guardrails

- Keep the deterministic core dependency-light and testable.
- LLM-assisted suggestions may be added later, but commit-blocking checks should remain deterministic.
- Prefer machine-readable JSON outputs for agent workflows.
- This repository is public. Read `CONTRIBUTING.md` before any commit, push, PR, issue, comment, or artifact upload.
- Use synthetic fixtures only. Keep private configuration, vault content, credentials, and audit reports outside the repository.
- Run the local publication gate before upload, including preflight of public prose. Never bypass a failed gate with `--no-verify`, `SKIP`, hook removal, or weakened configuration; investigate privately.
- CI and draft PRs are not private review surfaces. A passing scan does not replace reviewing the exact outgoing content.

## Dev commands

```bash
uv run pytest -q
uv run ruff check .
uv run wikic doctor --root . --json
uv run wikic lint --candidates --vault /path/to/vault --json
```
