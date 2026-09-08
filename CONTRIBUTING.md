# Contributing

## Public-by-default rule

Treat every commit pushed to GitHub—and every pull-request title, body, diff, comment, Actions log, and uploaded artifact—as immediately public and permanently copyable. CI runs after a push reaches GitHub, so it is a backstop, not a privacy boundary.

Never include credentials, private customer or employer information, personal filesystem paths, internal hostnames, private repository names, or real sensitive values in fixtures. Build synthetic test values at runtime when a detector test needs secret-like or path-like input.

## Local setup

```bash
uv sync --locked --all-groups
uv tool install pre-commit
pre-commit install --hook-type pre-commit --hook-type pre-push
```

The pre-commit stage scans staged content. The pre-push stage scans repository history and commit messages before transmission. Hooks can be bypassed, so run the complete validation command before opening a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
pre-commit run --all-files
pre-commit run --hook-stage pre-push --all-files
```

Keep sensitive work on an unpushed local branch until it has been reduced to a publication-safe change. Do not use a public draft pull request as a private review surface.

## Pull-request safety

- Keep changes narrow and deterministic.
- Do not expose repository secrets to pull-request code.
- Do not add `pull_request_target` workflows that check out or execute untrusted contributor code.
- Use synthetic fixtures and redact logs.
- Report vulnerabilities through the private process in `SECURITY.md`, not a pull request.
