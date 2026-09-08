# Security Policy

## Reporting a vulnerability

Please do not report suspected vulnerabilities in a public issue, discussion, or pull request.

Use GitHub's private vulnerability reporting flow from the repository's **Security** tab. Include:

- the affected command or component;
- reproduction steps or a minimal proof of concept;
- the expected and observed behavior;
- the potential impact;
- any suggested mitigation.

If private vulnerability reporting is unavailable, open a minimal issue asking the maintainers to provide a private contact channel. Do not include exploit details or sensitive data in that issue.

## Security model

Wikic reads Markdown vaults and can promote reviewed candidate content into them. Candidate identifiers and target paths are validated before reads, writes, or deletion. Treat candidate files and vault content as untrusted input, use `--dry-run` before applying changes, and keep backups for important vaults.

The repository runs full-history credential scanning and a separate identity/path sweep. These are defense-in-depth controls, not permission to commit secrets. Any exposed credential must be rotated immediately even if it is subsequently removed from Git history.
