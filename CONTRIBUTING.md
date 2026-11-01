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
- Board behavior must be tested against both adapters: Azure DevOps and the native Pam board. Parametrize board tests over both backends, or add a matching test per backend.
- Keep secrets out of the repo: names only in `.env.example`, values only in your local data directory.
- No em dashes in docs, UI text, or generated messages.
- Update CHANGELOG.md under Unreleased when user-visible behavior changes.

## Adding a new board adapter

One file per adapter. Implement the `PMAdapter` contract in `src/pam_pm/adapters/pm/base.py` (`list_items`, `get_item`, `create_item`, `update_item`, `delete_item`, `list_states`, `close_item`, iterations, links, comments, `poll_changes`) in one new file under `src/pam_pm/adapters/pm/`, with no changes to callers. Set the capability flags (`supports_dependency_links`, `supports_iterations`, `supports_comments`) so the UI hides what the backend cannot do. Use the shared `is_closed` helper for done states instead of comparing state strings. Add tests with the same fake-based coverage as the existing adapters, and document the backend in `docs/adapters.md`.

## Impeccable project scope

Console screens are checked with Impeccable (https://github.com/pbakaus/impeccable). Project scope for checks: the FastAPI console templates in `src/pam_pm/web/templates/` and styles in `src/pam_pm/web/static/`. Run the detector over that scope after changing console markup or CSS, and fix findings before pushing. Screenshots in `docs/images/` are regenerated with `tests/test_console_shots.py` when screens change.

## Pull requests

Fill in the template: what changed, how it was tested, and which docs were updated. One feature per PR. Maintainers review against the PRD in `docs/design/PRD.md`.
