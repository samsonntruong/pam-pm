# Teams adapter

Teams is two-way via Microsoft Graph polling, not bot callbacks. Pam
checks watched 1:1 chats and channels on the shared poll interval, so
no public endpoint and no inbound ports are needed.

## How it fits together

- Send path: Bot Framework REST (`TeamsAdapter.send_dm` and
  `send_buttons`) using the App ID, client secret, and Tenant ID from
  the wizard Teams step.
- Sign-in: Entra device code flow (`teams_auth`). The wizard shows a
  user code plus the microsoft.com/devicelogin link, polls for
  completion, then stores the refresh token in the secrets store
  (0600, never logged). Access tokens are minted silently from it and
  cached until near expiry (about one hour).
- Inbound: Graph polling (`teams_poll.TeamsPoller`). Each watched
  chat keeps a per-chat watermark timestamp in SQLite; every poll
  fetches only newer messages. A message becomes inbound when it
  mentions Pam or lands in a watched 1:1 chat. Matches publish onto
  the same event bus Slack messages use, and flow through
  `router.handle_teams_event` like Slack events. Proposals and
  approvals follow the unchanged shared code.
- Poll mode: `TeamsAdapter.attach_poller` switches the adapter to
  poll mode, where `poll_once` drains watched chats into the same
  inbound queues `read_replies` serves.

## Admin consent

Before signing in, a Microsoft 365 admin must consent once to the
Chat and Channel message permissions on the app registration. Without
that consent, sign-in succeeds but polling returns nothing. The
wizard states this plainly before the sign-in step.

## Watched chats

The poller takes an explicit watch list of chat ids (1:1 plus group
chats) and team plus channel pairs. Channel messages only become
inbound when they mention Pam; 1:1 messages always do. Pam's own
sends and system messages are skipped, and message ids already seen
are never emitted twice.

## Tests

`tests/test_teams_polling.py` covers the device flow against a fake
token endpoint, watermark advance, mention detection, DM detection,
dedupe on poll, silent token reuse, and secrets redaction. No test
touches the network; Graph and Entra calls use fakes.
