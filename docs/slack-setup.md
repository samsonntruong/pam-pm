# Slack setup

One internal Slack app per team, created once by the team lead or a workspace admin from the repo manifest. Everyone else installs nothing and just gets DMs from Pam. If the workspace restricts app installs, an admin approves it. Pam never ships or documents a shared distributed bot, and it never relies on `conversations.history`: it keeps its own conversation store.

## 1. Create the app from the manifest

1. Open [api.slack.com/apps](https://api.slack.com/apps) and choose Create New App, then From an app manifest.
2. Pick the team's workspace and paste the full contents of `slack-app-manifest.yaml` from the repo root.
3. Create the app, then install it to the workspace.

The manifest requests Socket Mode plus these bot scopes: `app_mentions:read`, `channels:read`, `chat:write`, `files:read`, `im:history`, `im:read`, `im:write`, `users:read`, `users:read.email`. It subscribes to `app_mention` and `message.im` events.

Note: the root `slack-app-manifest.yaml` is the source of truth. Do not edit scopes in the dashboard without updating the file, or the next install will lose them.

## 2. Copy the two tokens

- Bot token (`xoxb-...`): from OAuth and Permissions after install. Set as `SLACK_BOT_TOKEN`.
- App token (`xapp-...`): from Basic Information, App-Level Tokens. This drives Socket Mode. Set as `SLACK_APP_TOKEN`.

Paste both into the setup wizard step for Slack. Secrets live only in the local data directory, never in the repo.

## 3. Test

Press Test in the wizard. It checks scopes and Socket Mode, then sends you a DM. If the DM arrives, Slack is done.

Works on the Slack Free plan (10 app limit, 90 day message visibility). Because Pam stores its own conversations, the 90 day window does not affect it.

## Common mistakes

- Using an app token where a bot token belongs: Web API calls reject app tokens. The bot token does the messaging; the app token only opens the Socket Mode connection.
- Installing to the wrong workspace: the app must live in the workspace where your team chats.
- Missing DM permission: reinstall the app after any scope change so the new scopes take effect.
