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

Each step has a Test button. When all are green, the console lands on the live Board.

1. Meta Model API: paste your key, base URL, and model ID. Test makes a call and shows the model and tier. Keep the Standard tier for real team text; Contributor is for development with fake data only.
2. Slack: the team lead or a workspace admin creates the one internal app once from `slack-app-manifest.yaml` (Socket Mode); everyone else installs nothing. Paste the bot token and app token. Test checks scopes and sends you a DM. Full steps: [slack-setup.md](slack-setup.md).
3. Azure DevOps: paste the org URL and PAT, then pick project, team, and current iteration from the dropdowns. Test verifies Work Items read and write and detects the Agile or Scrum template. Full steps: [azure-devops-setup.md](azure-devops-setup.md).
4. Roster: teammates are pulled from ADO and auto-matched to Slack users by email. Fix mismatches, then set time zones and working hours.
5. Rules: toggle the morning check-in, follow-up on close, and blocker intake templates. Set times, days, and the review channel.
6. Safety: dry run stays on by default. Set the daily spend cap.
7. Finish: press "send me a test check-in" and answer it in Slack.

## 4. Verify from the terminal

```sh
pam doctor    # checks Meta, Slack, ADO config plus the prompt cache prefix
pam review    # prints a sample sprint review markdown
pam --version
```

`pam doctor` reports `doctor: ok` only when every check passes. If anything is missing it names the exact variable, for example `meta: missing META_API_KEY`.

## 5. Console screens

Left navigation: Setup, Rules, Board, Activity, Plan, Review, with a project switcher at the top.

- Rules: toggle templates per project.
- Board: read-only ADO board with dependency links, updating live.
- Activity: live feed of agent actions with evidence quotes and an Undo button for board writes.
- Plan: upload a sticky-note photo, inspect masks and readings, Approve to create items.
- Review: run the sprint review and create retro actions in ADO.

No hand-edited config files and no hunting for IDs. If a step fails, start with `pam doctor`, then see [troubleshooting.md](troubleshooting.md).
