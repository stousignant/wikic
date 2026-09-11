# Contributing

## Public-by-default rule

Treat every commit pushed to GitHub—and every pull-request title, body, diff, comment, Actions log, and uploaded artifact—as immediately public and permanently copyable. CI runs after a push reaches GitHub, so it is a backstop, not a privacy boundary.

Never include credentials, private customer or employer information, personal filesystem paths, internal hostnames, private repository names, or real sensitive values in fixtures. Build synthetic test values at runtime when a detector test needs secret-like or path-like input.

## Local setup

```bash
uv sync --locked --all-groups
uv tool install pre-commit
# Install a reviewed gitleaks binary (the version/checksum pins are in Security CI).
python3 scripts/install_publication_hooks.py --dry-run
python3 scripts/install_publication_hooks.py
```

The installer requires gitleaks on `PATH`, or an explicit reviewed binary supplied with `--gitleaks-executable`. It records that executable and installs a snapshot of the reviewed scanner in the shared Git directory. Commit and push gates run that snapshot, not branch-local code or pre-commit configuration. Older linked worktrees therefore receive the same gate without changing their tracked files. Reinstall deliberately after reviewing scanner updates.

The helper preserves every ref-update record supplied by Git, including pushes that update several branches or tags. It refuses to overwrite non-owned hooks by default. Review existing hooks first; `--replace-existing` explicitly replaces them with private backups. Do not use a generic `pre-commit install` to overwrite these installed publication hooks.

The staged gate scans the index, not unstaged working-tree replacements. The push gate checks outgoing Git content and metadata before transmission, including historical files, commit messages and annotated tags. Both installed gates include credential scanning. The optional pre-commit commands below use their separately pinned gitleaks environment for manual checks.

### Private rules shared by worktrees

Built-in rules detect structural home-directory paths, not every kind of personal information. Keep any additional regular expressions **outside the tracked tree** in the repository's shared Git directory:

```bash
git rev-parse --git-common-dir
```

Create `info/leakpatterns` under that directory using a local editor, one regular expression per line. Blank lines and `#` comments are ignored. Restrict the file's permissions. Never commit the rule list or paste it into public diagnostics.

For a maintainer publication workflow that depends on those private rules, enable the local requirement:

```bash
git config --local leakSweep.requirePatterns true
```

This setting and the shared file apply across linked worktrees. Missing, empty, unreadable or invalid required rules block the scan. Generic contributors and public CI can use the built-in rules without private configuration. A new clone does not inherit local configuration: configure it deliberately before publishing.

The legacy worktree-local `.leakpatterns` file is no longer the source of truth. Move reviewed private rules to the shared location before enabling the requirement. Do not supply private rules to public pull-request code through Actions secrets.

## Validation and publication

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
pre-commit run --all-files
python3 scripts/leak_sweep.py --history
```

The history command above checks privacy patterns. Add `--credentials` when running with gitleaks on `PATH` to scan credentials too. Installed commit/push hooks include credential scanning through the executable selected at installation.

Before each push, review the exact outgoing commits, paths and metadata. Then use normal `git push` so the installed gate receives the real ref updates. A manually invoked generic pre-push stage is not a substitute for those update records.

### Preflight public prose

Git hooks do not intercept GitHub API calls. Before creating or editing a PR, issue or comment:

1. Prepare the exact text in a local file outside the repository. Include the title in the preflight input as well as the body. Review attachments separately; text scanning does not certify an image or archive.
2. Review it for confidential context that pattern matching cannot recognize.
3. Scan that file locally:

   ```bash
   LEAK_SWEEP_TEXT_FILE="$file" \
     pre-commit run publication-sweep-text --hook-stage manual --all-files
   ```

4. Publish only the reviewed text. If it changes, rerun the check. Do not paste local diagnostic reports or scanner output into the public body.

Direct use is available with gitleaks on `PATH`:

```bash
python3 scripts/leak_sweep.py --text-file "$file"
```

A passing check means no configured pattern was detected, not that the text is guaranteed private-information-free.

## Failures and limits

- Scanner failures and unsupported/non-UTF-8 publication inputs block rather than silently passing. Binary assets require a separately reviewed policy; do not disable the gate to upload them.
- Public diagnostics contain no matched text or surrounding context. Investigate failures locally; never upload raw credential findings to Actions logs or artifacts.
- Ignore rules reduce accidental staging but do not protect already tracked files or forced additions. Example configuration files still require review.
- Never bypass a failed gate using `--no-verify`, `SKIP`, hook removal or weakened configuration. Stop and investigate privately.
- Hooks can be modified or bypassed by someone with sufficient local access. They are defense in depth, not a sandbox or a separate publishing authority.
- Keep sensitive work on an unpushed local branch until it has been reduced to a publication-safe change. A public draft PR is not private.

## Pull-request safety

- Keep changes narrow and deterministic.
- Do not expose repository secrets to pull-request code.
- Do not add `pull_request_target` workflows that check out or execute untrusted contributor code.
- Use synthetic fixtures and safe logs.
- Report vulnerabilities through the private process in `SECURITY.md`, not a public issue or pull request.
