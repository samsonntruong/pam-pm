# Contributing

Thanks for helping with Pam. Small focused pull requests beat large ones.

## Setup

```sh
pip install -e ".[dev]"
pytest -q
ruff check src tests
```

Python 3.12 or newer is required.

## Before every push

- Run `pytest -q` and `ruff check src tests`. Both must be clean.
- Add or update a test for each behavior change. Features use fake adapters, never live Slack or ADO.
- Keep secrets out of the repo: names only in `.env.example`, values only in your local data directory.
- No em dashes in docs, UI text, or generated messages.
- Update CHANGELOG.md under Unreleased when user-visible behavior changes.

## Pull requests

Fill in the template: what changed, how it was tested, and which docs were updated. One feature per PR. Maintainers review against the PRD in `docs/design/PRD.md`.
