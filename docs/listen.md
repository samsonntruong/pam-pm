# Listen

Capture a meeting from the browser, stream transcripts live, then generate notes with an approval card. Listen page: `/listen?project=<name>`. Meetings list: `/meetings?project=<name>`.

Flow: press Start listening, grant mic access, speak. Partials stream to the page. Finals persist as segments. Press Stop, open Review transcript and notes, press Generate notes, then Approve and create action items.

## Support matrix

Audio capture uses `getUserMedia` plus `AudioContext` plus `AudioWorklet`. The worklet resamples mic audio to 24 kHz mono PCM16 and the page sends 80 ms frames (3840 bytes) over `/ws/listen`.

| Browser | Capture | Notes |
| --- | --- | --- |
| Chrome 120+ desktop | Supported | Preferred. Fake media flags work for tests. |
| Edge 120+ desktop | Supported | Same engine as Chrome. |
| Firefox 120+ desktop | Best effort | Worklet supported, mic permission wording differs. |
| Safari 17+ desktop | Best effort | Requires mic permission per session, resample path same. |
| Mobile browsers | Not supported | No layout or mic tuning for mobile in beta. |
| HTTP without TLS | Mic blocked | Use `http://127.0.0.1` local or HTTPS. Other plain HTTP hosts block mic. |

Server transport: Meta realtime endpoint `wss://api.meta.ai/v1/asr/realtime`, model `muse-voice-transcribe-1.0`, mode `ENDPOINTING`, partial mode `CUMULATIVE`. Reconnect keeps 30 seconds of sent audio and resends once. Session cap is 2 hours, then the server sends `max_length` and stops.

Query params on `/ws/listen`: `token` (console token, required), `project`, `title`, `session_id`, `mode` (default `PUSH_TO_TALK` on the socket, realtime handshake defaults to `ENDPOINTING`).

## Consent

- One party presses Start listening in a shared browser session. Tell the room before you start. Suggested line: "Pam is transcribing this meeting for notes. Say now if you want it off."
- The Listen page shows a live Transcribing pill while audio flows. Stopping ends capture and closes the stream.
- Notes generation is manual. Nothing is summarized until someone presses Generate notes on the meeting page.
- Board writes need approval. Action items are created only after Approve and create action items on the approval card.
- Slack notify and channel post are opt in on the approval card. Notify defaults on, channel post defaults off.
- Delete a meeting any time with `DELETE /api/meetings/<session_id>`. That removes the session, segments, and notes.

## Storage

SQLite in the app data dir (see `Config.db_path`, override with `PAM_HOME`):

- `listen_sessions`: one row per capture (`session_id`, `project`, `backend`, `title`, `status`).
- `listen_segments`: one row per final (`idx`, `text`, `speaker`, `start_ms`, `end_ms`, `turn_id`).
- `listen_notes`: one row per notes run (`payload` JSON, `markdown`, `status` draft or approved).

What is stored: final text only, plus notes JSON and markdown after Generate notes. What is not stored: raw audio, partial hypotheses, mic stream. Audio frames are relayed to transcription and dropped, except the in memory 30 second resend buffer which never touches disk.

Key handling: the browser never sees `META_API_KEY`. The page carries the console token only. The server injects the Meta key into the realtime handshake and redacts it from logs with `redact_for_log`.

Retention: meetings stay until deleted from the API. There is no auto purge in beta. Keep audio stays off and has no effect in this build.

## Cost

Two meters:

- Transcription: `transcription_cost` in `src/pam_pm/models/realtime.py` at 3.00 USD per 1000 audio minutes. That is 0.00005 USD per audio second. A 30 minute meeting costs about 0.09 USD. Silence sent to hold the stream counts as audio.
- Notes: one Responses call through `ModelClient.chat` with prefix stable messages (`build_notes_messages`). Standard rates 1.25 input / 0.15 cached / 4.25 output per million tokens. Contributor rates 0.10 / 0.002 / 0.20. Contributor tier is for fake data runs only.

Spend cap: the console daily cap still applies to notes calls. Transcription spend is estimated client side and not auto logged to the calls table in beta, so check the provider dashboard for the billed figure.

## Troubleshooting

- Mic blocked: allow mic for the site, reload, press Start again. On plain HTTP use 127.0.0.1 or HTTPS.
- Audio start failed: use a recent Chrome or Edge. Check the worklet loads at `/static/listen-worklet.js`.
- Could not reach transcription: check `META_API_KEY` on the server, then retry. The page error names this case.
- Connection lost: the client resends up to 30 seconds. If finals stop, press Stop and start a new session.
- Empty transcript: speak for a few seconds after Transcribing appears. Very short clips may return no final.
- No notes to approve: press Generate notes first, then approve from the card it renders.
