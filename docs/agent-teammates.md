# Agent teammates

Design only; orchestration is out of MVP scope. This page records the direction so later work starts from the MVP contracts.

## Idea

Today Pam interviews people and syncs the board. Agent teammates would close the loop further: small scoped agents that take a draft action (triage a bug, prepare a review summary, chase a stale item) and report back through the same evidence log and Undo path a human answer uses.

## Constraints carried over from MVP

- Same write layer: dry run default, evidence quote per write, Undo for reversible writes.
- Same privacy rule: raw content stays with the agent run; the lead sees synthesized status and board writes.
- Same cost discipline: cached prompt prefixes, per-call token logging, daily spend cap.
- Structured handoffs validated with pydantic; retry once on failure.

## What exists today

The interviewer, lead synthesis, and planner agents plus the three rule templates. No cross-agent orchestration, no agent roster, no delegation UI.
