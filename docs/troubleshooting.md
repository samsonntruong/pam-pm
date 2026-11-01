# Troubleshooting

Start with `pam doctor` and the setup wizard Test buttons. They name the exact missing variable or failing check.

## pam doctor reports missing variables

- `meta: missing META_MODEL, META_BASE_URL, META_API_KEY`: fill the Meta step in the wizard. All three are required.
- `slack: missing SLACK_BOT_TOKEN`: paste the bot token (`xoxb-...`), not the app token. Web API calls reject app tokens.
- `ado: missing ADO_ORG_URL, ADO_PROJECT, ADO_PAT`: check the org URL spelling (`https://dev.azure.com/myorg`) and that the PAT has Work Items read and write plus Project and Team read. Only needed when Azure DevOps is the chosen backend.
- `cache prefix: unstable` or `error`: the prompt builder changed shape. Reinstall from a clean checkout and report it; do not edit prompts mid-run.

## Wizard Test button stays red

- Provider: confirm the key, base URL, and model ID match the provider console. A small `max_tokens` can return empty content because reasoning tokens are spent first; the wizard uses a generous budget. For a custom endpoint, confirm it supports chat, tools, structured output, and image input.
- Slack: confirm the app is installed to the right workspace, tokens are pasted into the right fields, and Socket Mode is enabled from the manifest. Reinstall the app after any scope change.
- ADO: confirm the PAT is not expired and the project dropdown resolved. If template detection looks wrong, check whether the project is Agile (`Closed`) or Scrum (`Done`) in Project Settings.

## pam start issues

- Port in use: `pam start --port 8770` and open the printed URL.
- Browser does not open: use `pam start --no-browser` and open the printed setup URL by hand.
- Invalid setup token: copy the full token from the terminal. It is single use per start; restart `pam start` for a fresh one.

## Board backend issues

- ADO step hidden: the Azure DevOps wizard step shows only when Azure DevOps is the chosen backend. Switch backends at the top of the Setup page.
- Wrong project backend: the backend choice is saved per project. Check the project switcher at the top of the console, then the backend label on the Setup page.

## Slack DM never arrives

- Confirm Socket Mode is enabled and the app token is set.
- Confirm the bot is installed in the workspace where you expect the DM.
- Check the Activity screen and server logs for errors; redact tokens before sharing logs.

## Board looks stale

- The poller runs every 60 seconds on ChangedDate. Wait one cycle, then refresh the Board screen.
- Confirm the item state spelling matches the template: Agile uses `Closed`, Scrum and the Pam board use `Done`.

## Listen page issues

- Mic blocked: allow mic for the site, reload, press Start again. On plain HTTP use 127.0.0.1 or HTTPS.
- Could not reach transcription: check `META_API_KEY` on the server, then retry. Custom providers do not support realtime transcription.
- Use Chrome or Edge on desktop. Full matrix: [listen.md](listen.md).

## Desktop window issues

- `pam desktop` needs the `[desktop]` extra installed. Reinstall with desktop support and try again.
- Tray icon missing on Linux: tray support varies by desktop environment; the console in the browser has every screen. See [desktop.md](desktop.md).

## Autostart issues

- Check status with `pam autostart status`. Re-enable with `pam autostart on`.
- After a reboot, `pam doctor` shows autostart status, last start, and uptime.

## Still stuck

Open an issue with the `pam doctor` output (it prints names only, never values), the failing wizard step, and which install path you used (installer, pipx, or source).
