# Azure DevOps setup

Connect Pam to one ADO organization, project, and team. The wizard detects the process template and maps states for you.

## 1. Create a PAT

1. In Azure DevOps, open User Settings, then Personal Access Tokens.
2. Create a token with Work Items read and write plus Project and Team read.
3. Copy the value once. Set it as `ADO_PAT` in the wizard.

Treat the PAT like a password. It lives only in the local data directory, never in the repo.

## 2. Fill in org, project, team

In the wizard step for Azure DevOps:

1. Paste the org URL as `ADO_ORG_URL`, for example `https://dev.azure.com/myorg`.
2. Paste the PAT.
3. Pick project, team, and current iteration from the dropdowns.

Press Test. It verifies Work Items read and write access and reports the detected template.

## 3. Agile vs Scrum states

Pam handles both process templates:

- Agile projects close work with the `Closed` state.
- Scrum projects close work with the `Done` state.

The adapter detects the template per project and maps open, active, and done states. Closed detection is shared through one `is_closed` helper, so the follow-up rule and the blocker watch treat `Closed` and `Done` the same way. You do not need to configure this by hand.

## 4. What Pam does in ADO

- Reads projects, teams, iterations, and items through WIQL.
- Updates state and remaining work, posts comments, and creates items.
- Creates dependency links with `System.LinkTypes.Dependency-Forward` (Successor) and `System.LinkTypes.Dependency-Reverse` (Predecessor).
- Polls for changes every 60 seconds on ChangedDate with a stored watermark, so handlers stay idempotent.

All writes start in dry-run mode. Turn dry run off per project only after the test check-in looks right.
