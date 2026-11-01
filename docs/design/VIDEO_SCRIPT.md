# VIDEO_SCRIPT: Pam demo video (2:50 max)

Record live on the Standard tier: real Slack with the throwaway test accounts, the real Pam board, and the real console. No em dashes in anything typed or shown. Total runtime 2:50 or less. Before shooting, verify the repo is public and replace the owner in the install line if it differs.

Slack test accounts (throwaway workspace only, tokens in ~/pam-secrets.env, never shown on screen):
- Test account 1 plays Ana, the teammate: gives the vague "almost done" answer, then the specific answer; later reports the blocker and receives the cleared notification.
- Test account 2 plays Ben, the blocking owner: receives the ETA request, replies with an ETA, then closes the blocking item.
- Samson plays the lead: runs the console, approves cards, narrates.

Use scripted replies, not model-generated ones. Keep each typed message exactly as written below.

## Beat 1: install (0:00-0:20)

On screen: a fresh Windows PowerShell window, full screen.

Action: paste the one-liner, press Enter, let it print each step. The browser opens the Pam setup console by itself.

Type exactly:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/samsonntruong/pam-pm/main/install.ps1 | iex"
```

Say: "One command installs Pam and opens the setup console."

## Beat 2: wizard chooses Pam board (0:20-0:45)

On screen: the setup wizard, starting at the backend choice.

Action: click the "Pam board" card (built in, recommended), click Continue. Step through Model provider (leave Meta default), Slack, People, Rules, Safety, pressing each Test button and showing the green pass. Do not type any secret on screen; cut past secret entry.

Say: "The wizard asks where tasks live. Pam board needs no outside account. Every step has a Test button."

## Beat 3: kickoff from a brief (0:45-1:10)

On screen: the Kickoff page with the brief box and the approval card.

Action: paste the brief below into the brief box, press Propose. Show the approval card with owner suggestions and reasons. Press Approve. Show the new items on the Board.

Type exactly (paste as the brief):

```
Sprint 12: ship the login form with tests, get API keys from the platform team, then deploy to staging. Ana owns the form, Ben owns the deploy.
```

Say: "Kickoff turns a short brief into tasks with suggested owners. Nothing is created until I approve."

## Beat 4: check-in pushback (1:10-1:35)

On screen: split view or quick cuts between Ana's Slack DM and the Activity screen.

Action: as Ana (test account 1), when Pam asks about the login form, reply first with the vague line, then after Pam pushes back, reply with the specific line. Show the board write with its evidence quote on the Activity screen.

Type exactly as Ana, first reply:

```
almost done
```

Type exactly as Ana, second reply after pushback:

```
Two tests left, about 3 hours, done by Thursday noon.
```

Say: "Pam pushes back on almost done until it has what remains and an ETA, then writes the board with Ana's own words as evidence."

## Beat 5: blocker link then clears (1:35-2:05)

On screen: Ana's Slack DM, then the Board showing the dependency link, then Ana's DM again for the notification.

Action: as Ana, report the blocker with the exact line. Answer Pam's two intake questions briefly. Show the Predecessor/Successor link on the Board. As Ben (test account 2), reply to the ETA request, then close the blocking item on the Board. Show Ana receiving the "blocker cleared" notification.

Type exactly as Ana:

```
Blocked on the API keys, waiting on Ben.
```

Type exactly as Ben (ETA reply):

```
Keys will be ready Friday 10am.
```

Say: "Pam links the blocker as a real dependency, asks Ben for an ETA, and tells Ana as soon as it clears."

## Beat 6: RAID log (2:05-2:20)

On screen: the RAID page with four tabs.

Action: open the RAID page, show one risk entry with owner and status linked to the API keys item. Add one assumption with the exact title below and save.

Type exactly as the assumption title:

```
Staging deploy needs no downtime window
```

Say: "Risks, assumptions, issues, and decisions live in one log with owners and links to work."

## Beat 7: review plus next sprint (2:20-2:50)

On screen: the Review page, then the next-sprint approval card.

Action: press Run review. Scroll the per-item review briefly, showing one low confidence flag. Show the retro actions. Open the next-sprint proposal card showing carryover plus backlog with owners and load. Press Approve. End on the Board with the new sprint.

Say: "Review writes itself from the week. The next sprint proposal carries over what is left. Approve, and sprint 13 is on the board. Pam: projects autonomously managed."

## Post-shoot checklist

- [ ] Runtime is 2:50 or less and plays while logged out.
- [ ] No secret, token, or real name visible in any frame.
- [ ] Upload to YouTube as Public or Unlisted, paste the link into SUBMISSION.md section 3.
