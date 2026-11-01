# Adapters

Pam talks to the outside world through four interfaces. Each interface has a real implementation for production and a fake for tests.

## ChatAdapter

- `slack.py`: production Slack over Socket Mode. DMs, threads, buttons, and file download. One internal app per team, created once by the team lead or a workspace admin; Pam keeps its own conversation store and never calls `conversations.history`.
- `fake.py`: in-memory chat for tests only. It is not a user-facing mode and the console never offers it.

## PMAdapter

Contract: `list_items`, `get_item`, `create_item`, `update_item`, `delete_item`, `list_states`, `close_item`, `list_iterations`, `create_iteration`, `set_item_iteration`, `add_link`, `list_links`, `list_comments`, `add_comment`, `poll_changes`. See `src/pam_pm/adapters/pm/base.py`.

- `azure_devops.py`: production ADO over REST 7.1. Items via WIQL, state and remaining work updates, comments, item creation, dependency links, team iterations, and change polling. Detects the Agile or Scrum template per project: Agile closes with `Closed`, Scrum closes with `Done`, and the shared `is_closed` helper treats both as closed. Iterations need `ADO_TEAM`.
- `jira.py`: production Jira Cloud over REST with email plus API token basic auth. Issues scoped to `JIRA_PROJECT`, workflow states with a To Do, In Progress, Done fallback, sprints through the Agile API, Blocks issue links, ADF comments, create and update, and changelog polling on `updated`. Owner writes need the Atlassian accountId; sprint readback needs the standard sprint field.
- `linear.py`: production Linear over GraphQL with an API key. Teams map to projects, workflow states in workflow order, cycles as iterations, blocks relations for dependencies, comments, create and update, and change polling on `updatedAt`. Owner writes match a user display name; label writes match existing team label names.
- `sqlite_board.py`: `NativeBoard`, Pam's own editable SQLite board. Same contract as ADO: full CRUD, states `New`, `Active`, `Blocked`, `Done` (done is `Done`), iterations with dates, dependency links, comments, tags, due date, owner, estimate, and remaining work, plus change polling with a `changed` watermark. Human edits through it are logged as human actions by the caller.

One file per adapter: each backend is one new file under `src/pam_pm/adapters/pm/` implementing `PMAdapter`, with no changes to callers. Jira and Linear are done; Asana and Monday are next.

Canonical done: `is_closed` is True for `Done` on Scrum, `Closed` on Agile, and `Done` on the native board (case-insensitive). Callers check `is_closed` instead of comparing state strings.

Canonical item fields in `WorkItem.fields`: `title`, `state`, `description`, `tags`, `due_date` (YYYY-MM-DD), `owner` (ADO spells it `assigned_to`), `estimate`, `remaining`, `iteration_id`.

### Capability flags

| Flag | NativeBoard | AzureDevOps | Jira | Linear |
| --- | --- | --- | --- | --- |
| `supports_dependency_links` | yes | yes | yes | yes |
| `supports_iterations` | yes | yes, needs `ADO_TEAM` | yes, sprints | yes, cycles |
| `supports_comments` | yes | yes | yes | yes |

The UI hides what a backend cannot do by reading these flags.

## ModelClient

One OpenAI-compatible client for Muse Spark with tool calling, structured output, and image input. All requests are assembled by `build_messages()` with a stable cached prefix and volatile data last. Tool choice stays `auto`; if the model answers without calling `finish_interview`, the caller retries once with a reminder, then runs a structured-output extraction call.

## VisionClient

SAM 3.1 segments sticky-note photos into masks; Muse Spark reads each crop and proposes a sprint. Time boxed: if SAM returns nothing usable, the whole photo goes to Muse Spark vision as the fallback path.

## Dependency links

ADO relation types: `System.LinkTypes.Dependency-Forward` (Successor) and `System.LinkTypes.Dependency-Reverse` (Predecessor). The blocker rule writes both sides so the Board view can render the chain.

Jira uses the `Blocks` issue link type; Linear uses `blocks` issue relations. Both adapters report `blocks` and `is blocked by`, and accept the ADO style aliases for the same directions.

## Backend config

Each backend resolves explicit args, then env, then wizard saved values. Tokens are never logged. Jira needs `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_PROJECT`, and `JIRA_API_TOKEN`. Linear needs `LINEAR_TEAM` and `LINEAR_API_KEY`. The wizard cards and Test buttons live on the setup page; the board stays read only for these backends with link outs to the source item.

## Trademarks

All trademarks are property of their respective owners; marks identify the services only.
