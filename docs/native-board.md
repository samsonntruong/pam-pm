# Pam board (native backend)

Pam ships with its own editable board, so teams without Azure DevOps can run every rule and screen with nothing else to configure. Pick it in the backend choice at the top of the Setup page. The choice is saved per project.

## What it supports

Full board contract: create, read, update, and delete items; states `New`, `Active`, `Blocked`, `Done`; iterations with dates; dependency links; comments; tags; due date, owner, estimate, and remaining work; plus change polling with a `changed` watermark. Rules run on it the same as on Azure DevOps: morning check-in, follow-up on close, blocker intake, triage, next sprint, and mid-sprint.

## Console

The Board screen is editable on the Pam board: drag cards between columns, open a card to edit fields, and add items inline. Human edits are logged as human actions on the Activity screen. Exports (CSV) live on the Board page.

## Data

Items live in the same SQLite store as the rest of Pam state, in the OS user data directory (see [install.md](install.md)). Back up that folder to back up the board. The RAID log keys entries by (backend, external_id), so each backend and project keeps its own log.

## Switching backends

Switching the backend choice changes where new reads and writes go. Items are not migrated between backends: ADO items stay in ADO, Pam board items stay local. Rules and the RAID log follow the current backend per project.
