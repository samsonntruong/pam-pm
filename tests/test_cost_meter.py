"""Cost meter: dollars and cached share from fake usage payloads."""

import sqlite3

import pytest

from pam_pm.core.cost import CallUsage, cached_share, dollars, log_call


def test_dollars_standard_with_cached_and_reasoning() -> None:
    usage = CallUsage(
        prompt_tokens=6000,
        cached_tokens=5000,
        completion_tokens=1500,
        reasoning_tokens=500,
        model="muse-spark-1.3",
        tier="standard",
    )
    # 1k x 1.25 + 5k x 0.15 + 1.5k x 4.25 per million, reasoning included.
    assert dollars(usage) == pytest.approx(0.008375)
    assert cached_share(usage) == pytest.approx(5000 / 6000)


def test_dollars_contributor_tier() -> None:
    usage = CallUsage(
        prompt_tokens=6000,
        cached_tokens=5000,
        completion_tokens=1500,
        reasoning_tokens=500,
        model="muse-spark-1.3-contributor",
        tier="contributor",
    )
    # 1k x 0.10 + 5k x 0.002 + 1.5k x 0.20 per million.
    assert dollars(usage) == pytest.approx(0.00041)
    assert cached_share(usage) == pytest.approx(5000 / 6000)


def test_cached_share_zero_prompt() -> None:
    assert cached_share(CallUsage(prompt_tokens=0)) == 0.0


def test_log_call_to_path_and_conn(tmp_path) -> None:
    usage = CallUsage(
        prompt_tokens=6000,
        cached_tokens=5000,
        completion_tokens=1500,
        reasoning_tokens=500,
        model="muse-spark-1.3",
        tier="standard",
    )
    db_path = tmp_path / "cost.db"
    assert log_call(db_path, usage) == 1
    conn = sqlite3.connect(str(db_path))
    try:
        assert log_call(conn, usage) == 2
        row = conn.execute(
            "SELECT model, tier, prompt_tokens, cached_tokens,"
            " completion_tokens, reasoning_tokens, dollars"
            " FROM calls ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert len(row) == 2
    assert row[0] == (
        "muse-spark-1.3",
        "standard",
        6000,
        5000,
        1500,
        500,
        pytest.approx(0.008375),
    )
