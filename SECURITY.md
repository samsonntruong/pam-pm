# Security

Found a vulnerability? Open a private security advisory on GitHub instead of a public issue. Include the affected version, steps to reproduce, and impact. We will confirm receipt and fix forward.

## Scope

Pam is a local-first app: console on 127.0.0.1, secrets in the local data directory, Slack over Socket Mode, ADO over polled HTTPS.

## Rules for reporters and contributors

- Secrets live only in the local data directory, never in the repo. See docs/privacy.md.
- Never paste tokens, PATs, or private Slack content into issues, PRs, or logs. Redact anything matching `xoxb-`, `xapp-`, `github_pat_`, or Bearer tokens.
- Masking runs before any model call; do not bypass it.

Supported versions: the latest commit on the default branch (alpha, v0.1.0 unreleased).
