# Adapters

Pam talks to the outside world through four interfaces. Each interface has a real implementation for production and a fake for tests.

## ChatAdapter

- `slack.py`: production Slack over Socket Mode. DMs, threads, buttons, and file download. One internal app per team, created once by the team lead or a workspace admin; Pam keeps its own conversation store and never calls `conversations.history`.
- `fake.py`: in-memory chat for tests only. It is not a user-facing mode and the console never offers it.

## PMAdapter

- `azure_devops.py`: production ADO over REST 7.1. Projects, teams, iterations, items via WIQL, state and remaining work updates, comments, item creation, dependency links, and change polling. Detects the Agile or Scrum template per project: Agile closes with `Closed`, Scrum closes with `Done`, and the shared `is_closed` helper treats both as closed.
- `sqlite_board.py`: local SQLite board for tests only. Not user facing.

## ModelClient

One OpenAI-compatible client for Muse Spark with tool calling, structured output, and image input. All requests are assembled by `build_messages()` with a stable cached prefix and volatile data last. Tool choice stays `auto`; if the model answers without calling `finish_interview`, the caller retries once with a reminder, then runs a structured-output extraction call.

## VisionClient

SAM 3.1 segments sticky-note photos into masks; Muse Spark reads each crop and proposes a sprint. Time boxed: if SAM returns nothing usable, the whole photo goes to Muse Spark vision as the fallback path.

## Dependency links

ADO relation types: `System.LinkTypes.Dependency-Forward` (Successor) and `System.LinkTypes.Dependency-Reverse` (Predecessor). The blocker rule writes both sides so the Board view can render the chain.
