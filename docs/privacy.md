# Privacy

Private by default. The team talks to Pam in Slack; the lead sees outcomes, not transcripts.

## Principles

- Raw answers stay between the person and the agent. The lead sees synthesized status, blockers, evidence quotes, and board writes.
- Masking runs before any model call: emails, phones, and tokens are redacted, and masking is covered by unit tests.
- Standard tier is the default model tier. Contributor tier may use inputs and outputs to improve Meta products, so it is used only for development with fake data.
- Secrets live only in the app data directory with restrictive file permissions, never in the repo. The repo carries `.env.example` with names only.
- Pam keeps its own conversation store in local SQLite. It never reads Slack history, so Slack retention limits do not affect it.

## Data flow

1. Slack DM arrives over Socket Mode; ADO state arrives through the poller.
2. Prompts are masked, then sent to the Meta Model API with the stable cached prefix.
3. Structured results land in SQLite with evidence quotes and cost records.
4. Board writes go through dry run first, then apply with an Undo record.

## Safety controls

- Dry-run default for all board writes.
- Evidence log: every write carries the quoted words behind it.
- Undo button for reversible writes on the Activity screen.
- Per-day spend cap with a hard stop.

To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## RAID logs

RAID entries carry the same evidence quote rules as board writes. Lead views show full quotes. Shared exports replace quotes with Lead only. Kickoff assumption proposals are logged with their source line as evidence.

## Listen meetings

- Consent: tell the room before pressing Start listening. The page shows a Transcribing pill while audio flows. Stopping ends capture.
- Audio: mic frames go to transcription and are not kept. Only final text lands in `listen_segments`. The 30 second resend buffer is memory only.
- Notes: generated only when someone presses Generate notes. Action items are created only after Approve on the meeting card.
- Keys: the browser never sees META_API_KEY. It stays server side in the realtime handshake and is redacted from logs.
- Retention: meetings stay until `DELETE /api/meetings/<session_id>` removes the session, segments, and notes. No auto purge in beta.
- Tiers: Contributor tier runs use fake data only. Do not capture real meetings on Contributor.

Details: [listen.md](listen.md).
