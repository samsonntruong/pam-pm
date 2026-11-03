# Setup

Install Pam, run the wizard in your browser, and reach a working dry run in under 10 minutes. No terminal steps after install. No config files to edit.

## 1. Install

Requires Python 3.12 or newer.

With pipx (recommended):

```sh
pipx install git+https://github.com/samsonntruong/pam-pm.git
pam start
```

From a clone (for development):

```sh
pip install -e ".[dev]"
pam start
```

If `pam` collides with another tool on your machine, use `pam-pm start`. Both entry points ship in the package.

For server hosting, see [deploy.md](deploy.md).

## 2. Open the wizard

`pam start` opens the wizard in your browser. If it does not open, use the URL it prints:

```text
URL: http://127.0.0.1:8765/setup?project=default
```

The wizard has seven short steps. Each step has Save plus Test. Fill the fields, choose Save, then choose Test. When all are green, open the Board. Progress saves per project as you go, so you can close the browser and come back later.

Where values live: non-secret settings are stored as plain JSON in the app data dir. Tokens and keys are stored one per file with 0600 permissions. Wizard progress stores only yes or no flags for secrets, never the values. Logs never include secret values.

Env override: if you set a wizard value in the environment (see .env.example for the full list), that value wins over the saved value. This is for servers and CI. On a laptop, leave them unset and use the wizard.

## 3. Work the wizard

Pick a board backend first, then work the numbered steps.

- Backend choice: Pam board (built in, nothing to configure), Azure DevOps, Jira, Linear, Trello, ClickUp, Asana, or Monday. Saved per project, so different projects can use different backends. See [native-board.md](native-board.md).
- Step 1, Model provider: pick Meta (default, recommended) or Custom OpenAI-compatible. For Meta, enter the model name, base URL, and API key, then Save and Test. For Custom, enter its base URL, model name, and key. Test uses plain words. Keep the Standard tier for real team text; Contributor is for development with fake data only. See [provider.md](provider.md).
- Step 2, Slack: the team lead or a workspace admin creates the one internal app once from `slack-app-manifest.yaml` (Socket Mode); everyone else installs nothing. Paste the bot token and app token, then Save and Test. Test checks that Pam can send messages. Full steps: [slack-setup.md](slack-setup.md). Keep the Tell your team note on that step.
- Step 2b, Teams (optional): two-way via Graph polling with Entra device code sign-in. Save the App ID, client secret, and Tenant ID, then Save and Test. See [teams-adapter.md](teams-adapter.md).
- Step 3, Board backend: shown only when your backend needs credentials (everything except Pam board). Enter the values shown for your backend, then Save and Test. Test checks that Pam can read the project. Full steps for ADO: [azure-devops-setup.md](azure-devops-setup.md).
- Step 4, People: add teammates who will get the morning message, one name per line. Choose Save, then Test.
- Step 5, Rules: choose which automatic messages you want on the Rules page, then Test.
- Step 6, Safety: dry run stays on by default. Review the daily spend cap, then Test.
- Step 7, Done: paste the one-time setup token printed by `pam start` to finish. The autostart box is checked by default so Pam starts at sign-in.

## Share on your network (team mode)

One machine hosts the Pam board and teammates open it in a browser. Nothing for them to install: they join the same Wi-Fi or network, open the LAN link from the Share section of the wizard, enter the console password, and land on the Board.

Use share mode for a quick co-located review or a small team on one trusted network. Use a shared backend (Azure DevOps, Jira, Linear, Trello, ClickUp, Asana, or Monday) for real teams that need remote access, per-user permissions, and history outside one laptop. With a shared backend each person works in the system of record and Pam stays on the host machine.

Security limits: share mode works on the same Wi-Fi or network only and has no encryption, so only use it on a network you trust. A console password is required to start shared mode, and anyone with the URL plus the password gets in. Keep the host machine on and Pam running while the team uses the board.

## Provider limits

Meta is the default transport and uses the Responses API. A custom OpenAI-compatible endpoint works for chat, tools, structured output, and image input when the endpoint supports them. SAM 3.1 planning is Meta only: with a custom provider the planner skips segmentation and reads the whole photo as one image. This choice is independent of the board backend and works with all eight board backends.

## 4. Confirm in the console

Stay in the browser. Open the Board and Activity pages and confirm your work shows up. If a step fails, read its hint on the page, then see [troubleshooting.md](troubleshooting.md).

Left navigation: Setup, Rules, Triage, Board, Activity, Plan, Kickoff, Add task, RAID, Next sprint, Mid-sprint, Review, Listen, Meetings, Settings, with a project switcher at the top.

- Rules: toggle templates per project.
- Triage: daily board scan findings with proposed fixes and Approve buttons.
- Board: your chosen backend with dependency links, updating live. Pam board items are editable.
- Activity: live feed of agent actions with evidence quotes and an Undo button for board writes.
- Plan: upload a sticky-note photo, inspect masks and readings, Approve to create items.
- Kickoff: paste a brief or upload a file, review owner suggestions with reasons on one approval card, Approve to create items.
- RAID: risks, assumptions, issues, and dependencies with accept, edit, close, and export.
- Next sprint and Mid-sprint: proposal cards with Approve buttons.
- Review: run the sprint review and create retro actions.
- Listen and Meetings: capture meeting audio, review transcripts, generate notes, approve action items.

Optional terminal checks: `pam doctor` and `pam review` exist but are not required. The wizard is the supported path.
