# Teams adapter

Supported path: use the setup wizard Teams step. Save keys, grant admin consent, then download the generated app package from the wizard and sideload it.

Teams is two-way via Microsoft Graph polling with app permissions, not bot callbacks and not user sign-in. Pam checks watched 1:1 chats and channels on the shared poll interval, so no public endpoint and no inbound ports are needed. The app-permissions route is the only flow. There is no refresh token and no sign-in step.

## Entra app registration

Create an app registration in Microsoft Entra ID (Azure portal, Entra ID, App registrations, New registration). Copy three values into the Pam wizard Teams step:

- Application (client) ID goes to Teams App ID.
- Directory (tenant) ID goes to Tenant ID.
- A client secret Value (Certificates and secrets, New client secret) goes to App Password. Copy the Value once; the ID is not enough.

Pam reads the saved values from the secrets store, with explicit env vars (TEAMS_APP_ID, TEAMS_APP_PASSWORD, TEAMS_TENANT_ID) winning when set. Secrets are never logged.

## Application permissions and admin consent

Polling uses Graph application permissions with client credentials:

- Chat.Read.All (read 1:1 and group chats)
- ChannelMessage.Read.All (read channel messages for mentions)

Add both as application permissions, then choose Grant admin consent once. Without consent, the token mint succeeds but every poll returns empty. Consent is per tenant; repeat it in each tenant that runs Pam.

## Privacy note

These permissions can read every chat in the tenant, so Pam only polls the configured watched chats and keeps per-chat watermark timestamps in SQLite. Message bodies are never stored. Only newer messages are fetched, and only matches reach the event bus.

## Bot install for sending

Sending uses Bot Framework REST with the same App ID, password, and tenant. Register a bot (Bot Service or the Teams developer portal), paste the same credentials, and install the bot to the teams and users Pam messages. The bot handle should be named Pam so channel mentions match the default mention filter.

Pam skips its own sends by sender id, skips system messages, and never emits the same message id twice.

## App token minting for polling

Polling mints Graph tokens with client credentials against the tenant token endpoint and the https://graph.microsoft.com/.default scope. Tokens are cached until near expiry (about one hour) and minted again with no user step. Each watched chat keeps a per-chat watermark timestamp in SQLite; every poll fetches only newer messages. A 1:1 message always becomes inbound; a channel message only when it mentions Pam. Matches publish to the same event bus Slack uses and flow through router.handle_teams_event.

Missing app ID, password, or tenant means the startup loop stays off for that project.

## Multi-tenant note

Use one app registration per tenant (single tenant), or one multi-tenant registration with admin consent in each tenant. Tenant ID selects the token endpoint; app tokens do not cross tenants. Each Pam project stores its own chat backend (slack or teams) and its own watermarks, so mixed estates poll each project on its own backend.

## Personal accounts unsupported

Personal Microsoft accounts are not supported. Polling needs a work or school account in a Microsoft 365 tenant with Teams, Chat, and Channel permissions plus admin consent. Personal accounts cannot grant the ChannelMessage.Read.All permission and cannot complete the required admin consent step.

## Tests

tests/test_teams_polling.py covers app token minting against a fake token endpoint, watermark advance, mention detection, DM detection, dedupe on poll, cached token reuse, the startup loop wrapper with a stub graph, and secrets redaction. No test touches the network; Graph and Entra calls use fakes.
