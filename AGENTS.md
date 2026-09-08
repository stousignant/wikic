# Wikic Agent Notes

Wikic is a deterministic Markdown/LLM-wiki compiler.

## Guardrails

- Keep the deterministic core dependency-light and testable.
- LLM-assisted suggestions may be added later, but commit-blocking checks should remain deterministic.
- Prefer machine-readable JSON outputs for agent workflows.

## Dev commands

```bash
uv run pytest -q
uv run ruff check .
uv run wikic doctor --root . --json
uv run wikic lint --candidates --vault /path/to/vault --json
```
