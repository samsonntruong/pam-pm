# Teams adapter

Supported path: use the setup wizard Teams step. Save keys, run Test,
then download the generated app package from the wizard and sideload it.

Teams is two-way via Microsoft Graph polling, not bot callbacks. Pam
checks watched 1:1 chats and channels on the shared poll interval, so
no public endpoint and no inbound ports are needed.

## Entra app registration

Create an app registration in Microsoft Entra ID (Azure portal, Entra ID,
App registrations, New registration). Copy three values into the Pam
wizard Teams step:

- Application (client) ID goes to Teams App ID.
- Directory (tenant) ID goes to Tenant ID.
- A client secret Value (Certificates and secrets, New client secret)
  goes to App Password. Copy the Value once; the ID is not enough.

Pam reads the saved values from the secrets store, with explicit env
vars (TEAMS_APP_ID, TEAMS_APP_PASSWORD, TEAMS_TENANT_ID) winning when
set. Secrets are never logged.

## Delegated permissions and admin consent

Polling uses delegated Graph permissions on behalf of the signed in
user:

- Chat.Read (read 1:1 and group chats)
- Chat.ReadWrite (read plus reply tracking where needed)
- ChannelMessage.Read.All (read channel messages for mentions)
- offline_access (keep the refresh token for silent hourly minting)

An admin must grant admin consent once (API permissions, Grant admin
consent). Without consent, device sign-in succeeds but every poll
returns empty. Consent is per tenant; repeat it in each tenant that
runs Pam.

## Bot install for sending

Sending uses Bot Framework REST with the same App ID, password, and
tenant. Register a bot (Bot Service or the Teams developer portal),
paste the same credentials, and install the bot to the teams and users
Pam messages. The bot handle should be named Pam so channel mentions
match the default mention filter.

Pam skips its own sends by sender id, skips system messages, and never
emits the same message id twice.

## Device sign-in for polling

Polling needs a delegated user sign-in via the Entra device code flow:

1. Save the App ID and Tenant ID in the wizard Teams step.
2. Choose Start sign-in. Pam shows a user code and the
   microsoft.com/devicelogin link.
3. Open the link on any device, enter the code, approve the sign-in.
4. Choose check status. Pam stores the refresh token (0600 file,
   never logged) and marks the project signed in.

Access tokens are minted silently from the refresh token and cached
until near expiry (about one hour). Rotated refresh tokens are stored
back immediately. Each watched chat keeps a per-chat watermark
timestamp in SQLite; every poll fetches only newer messages. A 1:1
message always becomes inbound; a channel message only when it
mentions Pam. Matches publish to the same event bus Slack uses and
flow through router.handle_teams_event.

No refresh token means the startup loop stays off for that project.

## Multi-tenant note

Use one app registration per tenant (single tenant), or one
multi-tenant registration with admin consent in each tenant. Tenant ID
selects the token endpoint; device codes do not cross tenants. Each
Pam project stores its own chat backend (slack or teams) and its own
watermarks, so mixed estates poll each project on its own backend.

## Personal accounts unsupported

Personal Microsoft accounts are not supported. Polling needs a work or
school account in a Microsoft 365 tenant with Teams, Chat, and Channel
permissions plus admin consent. Personal accounts cannot grant the
ChannelMessage.Read.All permission and cannot complete the required
admin consent step.

## Tests

tests/test_teams_polling.py covers the device flow against a fake
token endpoint, watermark advance, mention detection, DM detection,
dedupe on poll, silent token reuse, rotated refresh storage, the
startup loop wrapper with a stub graph, and secrets redaction. No test
touches the network; Graph and Entra calls use fakes.
