# Setup

Install Pam, run the wizard, and reach a working dry run in under 10 minutes.

## 1. Install

Requires Python 3.12 or newer.

With pipx (recommended):

```sh
pipx install git+https://github.com/OWNER/pam-pm.git
pam start
```

From a clone (for development):

```sh
pip install -e ".[dev]"
pam start
```

If `pam` collides with another tool on your machine, use `pam-pm start`. Both entry points ship in the package.

For server hosting, see [deploy.md](deploy.md).

## 2. Start the console

`pam start` binds to 127.0.0.1 port 8765 by default:

```sh
pam start --host 127.0.0.1 --port 8765
pam start --no-browser   # print the URL without opening a browser
```

It prints a one-time setup token and the wizard URL in this form:

```text
Setup token: <token>
URL: http://127.0.0.1:8765/setup?token=<token>
```

Open that URL in your browser. The token is required to complete setup.

## 3. Work the wizard

Pick a board backend first, then work the numbered steps. Each step has a Test button. When all are green, the console lands on the live Board. Progress saves per project as you go.

- Backend choice: Pam board (built in, nothing to configure) or Azure DevOps. Saved per project, so different projects can use different backends. See [native-board.md](native-board.md).
1. Model provider: pick Meta (default, recommended) or Custom OpenAI-compatible. Meta uses META_MODEL, META_BASE_URL, and META_API_KEY from your secrets file. Custom needs a base URL, model name, and API key stored only on this machine. Test checks tool calling, structured output, and image input in plain words. Keep the Standard tier for real team text; Contributor is for development with fake data only. See [provider.md](provider.md).
2. Slack: the team lead or a workspace admin creates the one internal app once from `slack-app-manifest.yaml` (Socket Mode); everyone else installs nothing. Paste the bot token and app token. Test checks scopes and sends you a DM. Full steps: [slack-setup.md](slack-setup.md).
3. Azure DevOps: shown only when Azure DevOps is chosen above. Paste the org URL and PAT, then pick project, team, and current iteration from the dropdowns. Test verifies Work Items read and write and detects the Agile or Scrum template. Full steps: [azure-devops-setup.md](azure-devops-setup.md).
4. People: add teammates who will get the morning message, one name per line. Names save with this step.
5. Rules: choose which automatic messages you want on the Rules page, then check.
6. Safety: dry run stays on by default. Review the daily spend cap, then check.
7. Done: paste the one-time setup token printed by `pam start` to finish. The autostart box is checked by default so Pam starts at sign-in.

## Provider limits

Meta is the default transport and uses the Responses API. A custom OpenAI-compatible endpoint works for chat, tools, structured output, and image input when the endpoint supports them. SAM 3.1 planning is Meta only: with a custom provider the planner skips segmentation and reads the whole photo as one image. Custom keys are stored only in the local data directory and never appear in logs or wizard progress. This choice is independent of the board backend and works with both the Pam board and Azure DevOps.

## 4. Verify from the terminal

```sh
pam doctor    # checks Meta, Slack, ADO config plus the prompt cache prefix
pam review    # prints a sample sprint review markdown
pam --version
```

`pam doctor` reports `doctor: ok` only when every check passes. If anything is missing it names the exact variable, for example `meta: missing META_API_KEY`.

## 5. Console screens

Left navigation: Setup, Rules, Triage, Board, Activity, Plan, Kickoff, Add task, RAID, Next sprint, Mid-sprint, Review, Listen, Meetings, Settings, with a project switcher at the top.

- Rules: toggle templates per project.
- Triage: daily board scan findings with proposed fixes and Approve buttons.
- Board: ADO or Pam board with dependency links, updating live. Pam board items are editable.
- Activity: live feed of agent actions with evidence quotes and an Undo button for board writes.
- Plan: upload a sticky-note photo, inspect masks and readings, Approve to create items.
- Kickoff: paste a brief or upload a file, review owner suggestions with reasons on one approval card, Approve to create items.
- RAID: risks, assumptions, issues, and dependencies with accept, edit, close, and export.
- Next sprint and Mid-sprint: proposal cards with Approve buttons.
- Review: run the sprint review and create retro actions.
- Listen and Meetings: capture meeting audio, review transcripts, generate notes, approve action items.

No hand-edited config files and no hunting for IDs. If a step fails, start with `pam doctor`, then see [troubleshooting.md](troubleshooting.md).
