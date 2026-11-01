"""Cost meter: per-call dollars, cached share, and SQLite logging (F9)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

# Rates are USD per million tokens: (plain input, cached input, output).
STANDARD_RATES = (1.25, 0.15, 4.25)
CONTRIBUTOR_RATES = (0.10, 0.002, 0.20)

CALLS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  model TEXT NOT NULL DEFAULT '',
  tier TEXT NOT NULL DEFAULT 'standard',
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  cached_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  dollars REAL NOT NULL DEFAULT 0
)"""


@dataclass
class CallUsage:
    """Token usage for one model call.

    completion_tokens is total output including reasoning, matching the
    provider usage shape. reasoning_tokens is the reasoning part of it,
    kept for reporting. If reasoning_tokens exceeds completion_tokens the
    two are treated as disjoint and both are billed as output.
    """

    prompt_tokens: int
    cached_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    model: str = ""
    tier: str = "standard"


def dollars(usage: CallUsage) -> float:
    """Return the USD cost of one call. Contributor tier uses its rates."""
    if usage.tier.lower() == "contributor":
        in_rate, cached_rate, out_rate = CONTRIBUTOR_RATES
    else:
        in_rate, cached_rate, out_rate = STANDARD_RATES
    uncached = max(0, usage.prompt_tokens - usage.cached_tokens)
    if usage.reasoning_tokens > usage.completion_tokens:
        output = usage.completion_tokens + usage.reasoning_tokens
    else:
        output = usage.completion_tokens
    total = (
        uncached * in_rate + usage.cached_tokens * cached_rate + output * out_rate
    )
    return total / 1_000_000


def cached_share(usage: CallUsage) -> float:
    """Return the fraction of input tokens served from cache, 0 to 1."""
    if usage.prompt_tokens <= 0:
        return 0.0
    return usage.cached_tokens / usage.prompt_tokens


def log_call(db: str | Path | sqlite3.Connection, usage: CallUsage) -> int:
    """Append one usage row to SQLite. Accepts a path or an open connection."""
    close = False
    if isinstance(db, sqlite3.Connection):
        conn = db
    else:
        conn = sqlite3.connect(str(db))
        close = True
    try:
        conn.execute(CALLS_TABLE_DDL)
        cur = conn.execute(
            "INSERT INTO calls (model, tier, prompt_tokens, cached_tokens,"
            " completion_tokens, reasoning_tokens, dollars)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                usage.model,
                usage.tier,
                usage.prompt_tokens,
                usage.cached_tokens,
                usage.completion_tokens,
                usage.reasoning_tokens,
                dollars(usage),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        if close:
            conn.close()
