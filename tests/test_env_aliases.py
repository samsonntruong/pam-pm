"""Env var aliases for PM connectors.

Each connector accepts an alias name for some config values while
the canonical name wins when both are set. All values here are fake
and no test touches the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pam_pm.adapters.pm.asana import AsanaAdapter, AsanaError
from pam_pm.adapters.pm.clickup import ClickUpAdapter, ClickUpError
from pam_pm.adapters.pm.jira import JiraAdapter, JiraError
from pam_pm.adapters.pm.linear import LinearAdapter, LinearError
from pam_pm.adapters.pm.monday import MondayAdapter, MondayError
from pam_pm.core.secrets import (
    effective_asana_config,
    effective_clickup_config,
    effective_jira_config,
    effective_linear_config,
    effective_monday_config,
    env_first,
)

FAKE_TOKEN = "fake-token-for-alias-test"
FAKE_EMAIL = "tester@example.com"

# system, config key, canonical, alias, canon value, alias value, alias expect.
# Jira project keys normalize to upper case, so its alias expectation differs.
CASES = [
    ("clickup", "team", "CLICKUP_TEAM", "CLICKUP_TEAM_ID",
     "canon-team-1", "alias-team-1", "alias-team-1"),
    ("clickup", "list", "CLICKUP_LIST", "CLICKUP_LIST_ID",
     "canon-list-1", "alias-list-1", "alias-list-1"),
    ("asana", "project", "ASANA_PROJECT", "ASANA_PROJECT_ID",
     "canon-project-1", "alias-project-1", "alias-project-1"),
    ("linear", "team", "LINEAR_TEAM", "LINEAR_TEAM_KEY",
     "canon-eng", "alias-eng", "alias-eng"),
    ("jira", "base_url", "JIRA_BASE_URL", "JIRA_SITE_URL",
     "https://canon-jira.example.com", "https://alias-jira.example.com",
     "https://alias-jira.example.com"),
    ("jira", "project", "JIRA_PROJECT", "JIRA_PROJECT_KEY",
     "CANONPROJ", "aliasproj", "ALIASPROJ"),
    ("monday", "board", "MONDAY_BOARD", "MONDAY_BOARD_ID",
     "canon-board-1", "alias-board-1", "alias-board-1"),
]

CASE_IDS = [case[2] for case in CASES]

ALL_ENV_VARS = [
    "CLICKUP_TEAM",
    "CLICKUP_TEAM_ID",
    "CLICKUP_LIST",
    "CLICKUP_LIST_ID",
    "CLICKUP_API_TOKEN",
    "ASANA_PROJECT",
    "ASANA_PROJECT_ID",
    "ASANA_API_TOKEN",
    "LINEAR_TEAM",
    "LINEAR_TEAM_KEY",
    "LINEAR_API_KEY",
    "JIRA_BASE_URL",
    "JIRA_SITE_URL",
    "JIRA_EMAIL",
    "JIRA_PROJECT",
    "JIRA_PROJECT_KEY",
    "JIRA_API_TOKEN",
    "MONDAY_BOARD",
    "MONDAY_BOARD_ID",
    "MONDAY_API_TOKEN",
]

ADAPTER_ATTRS = {
    ("clickup", "team"): "_team_ref",
    ("clickup", "list"): "_list_ref",
    ("asana", "project"): "_project_ref",
    ("linear", "team"): "_team_key",
    ("jira", "base_url"): "_base",
    ("jira", "project"): "_project",
    ("monday", "board"): "_board_ref",
}


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for var in ALL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _effective(system: str, data_dir: Path) -> dict[str, Any]:
    if system == "clickup":
        return dict(effective_clickup_config(data_dir))
    if system == "asana":
        return dict(effective_asana_config(data_dir))
    if system == "linear":
        return dict(effective_linear_config(data_dir))
    if system == "jira":
        return dict(effective_jira_config(data_dir))
    return dict(effective_monday_config(data_dir))


def _support(monkeypatch: pytest.MonkeyPatch, system: str, field: str) -> None:
    """Set the other fake values one adapter needs to construct."""
    if system == "clickup":
        monkeypatch.setenv("CLICKUP_API_TOKEN", FAKE_TOKEN)
        if field == "team":
            monkeypatch.setenv("CLICKUP_LIST", "canon-list-1")
        else:
            monkeypatch.setenv("CLICKUP_TEAM", "canon-team-1")
    elif system == "asana":
        monkeypatch.setenv("ASANA_API_TOKEN", FAKE_TOKEN)
    elif system == "linear":
        monkeypatch.setenv("LINEAR_API_KEY", FAKE_TOKEN)
    elif system == "jira":
        monkeypatch.setenv("JIRA_EMAIL", FAKE_EMAIL)
        monkeypatch.setenv("JIRA_API_TOKEN", FAKE_TOKEN)
        if field == "base_url":
            monkeypatch.setenv("JIRA_PROJECT", "CANONPROJ")
        else:
            monkeypatch.setenv("JIRA_BASE_URL", "https://canon-jira.example.com")
    else:
        monkeypatch.setenv("MONDAY_API_TOKEN", FAKE_TOKEN)


def _adapter(system: str, data_dir: Path) -> Any:
    if system == "clickup":
        return ClickUpAdapter(data_dir=data_dir)
    if system == "asana":
        return AsanaAdapter(data_dir=data_dir)
    if system == "linear":
        return LinearAdapter(data_dir=data_dir)
    if system == "jira":
        return JiraAdapter(data_dir=data_dir)
    return MondayAdapter(data_dir=data_dir)


@pytest.mark.parametrize(
    "system,field,canonical,alias,canon_value,alias_value,expected",
    CASES,
    ids=CASE_IDS,
)
def test_alias_only_resolves_config(
    clean_env: pytest.MonkeyPatch,
    tmp_path: Path,
    system: str,
    field: str,
    canonical: str,
    alias: str,
    canon_value: str,
    alias_value: str,
    expected: str,
) -> None:
    clean_env.setenv(alias, alias_value)
    assert _effective(system, tmp_path)[field] == expected


@pytest.mark.parametrize(
    "system,field,canonical,alias,canon_value,alias_value,expected",
    CASES,
    ids=CASE_IDS,
)
def test_canonical_wins_config(
    clean_env: pytest.MonkeyPatch,
    tmp_path: Path,
    system: str,
    field: str,
    canonical: str,
    alias: str,
    canon_value: str,
    alias_value: str,
    expected: str,
) -> None:
    clean_env.setenv(canonical, canon_value)
    clean_env.setenv(alias, alias_value)
    assert _effective(system, tmp_path)[field] == canon_value


@pytest.mark.parametrize(
    "system,field,canonical,alias,canon_value,alias_value,expected",
    CASES,
    ids=CASE_IDS,
)
def test_alias_only_resolves_adapter(
    clean_env: pytest.MonkeyPatch,
    tmp_path: Path,
    system: str,
    field: str,
    canonical: str,
    alias: str,
    canon_value: str,
    alias_value: str,
    expected: str,
) -> None:
    clean_env.setenv(alias, alias_value)
    _support(clean_env, system, field)
    adapter = _adapter(system, tmp_path)
    try:
        assert getattr(adapter, ADAPTER_ATTRS[(system, field)]) == expected
    finally:
        adapter.close()


@pytest.mark.parametrize(
    "system,field,canonical,alias,canon_value,alias_value,expected",
    CASES,
    ids=CASE_IDS,
)
def test_canonical_wins_adapter(
    clean_env: pytest.MonkeyPatch,
    tmp_path: Path,
    system: str,
    field: str,
    canonical: str,
    alias: str,
    canon_value: str,
    alias_value: str,
    expected: str,
) -> None:
    clean_env.setenv(canonical, canon_value)
    clean_env.setenv(alias, alias_value)
    _support(clean_env, system, field)
    adapter = _adapter(system, tmp_path)
    try:
        assert getattr(adapter, ADAPTER_ATTRS[(system, field)]) == canon_value
    finally:
        adapter.close()


def test_env_first_skips_blank_and_missing(
    clean_env: pytest.MonkeyPatch,
) -> None:
    assert env_first("CLICKUP_TEAM", "CLICKUP_TEAM_ID") == ""
    clean_env.setenv("CLICKUP_TEAM", "   ")
    clean_env.setenv("CLICKUP_TEAM_ID", "alias-team-1")
    assert env_first("CLICKUP_TEAM", "CLICKUP_TEAM_ID") == "alias-team-1"
    clean_env.setenv("CLICKUP_TEAM", "canon-team-1")
    assert env_first("CLICKUP_TEAM", "CLICKUP_TEAM_ID") == "canon-team-1"


def test_required_errors_keep_canonical_name(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean_env.setenv("LINEAR_API_KEY", FAKE_TOKEN)
    with pytest.raises(LinearError, match="LINEAR_TEAM is required"):
        LinearAdapter(data_dir=tmp_path)
    clean_env.setenv("ASANA_API_TOKEN", FAKE_TOKEN)
    with pytest.raises(AsanaError, match="ASANA_PROJECT is required"):
        AsanaAdapter(data_dir=tmp_path)
    clean_env.setenv("MONDAY_API_TOKEN", FAKE_TOKEN)
    with pytest.raises(MondayError, match="MONDAY_BOARD is required"):
        MondayAdapter(data_dir=tmp_path)
    clean_env.setenv("CLICKUP_API_TOKEN", FAKE_TOKEN)
    clean_env.setenv("CLICKUP_TEAM", "canon-team-1")
    with pytest.raises(ClickUpError, match="CLICKUP_LIST is required"):
        ClickUpAdapter(data_dir=tmp_path)
    clean_env.setenv("JIRA_EMAIL", FAKE_EMAIL)
    clean_env.setenv("JIRA_API_TOKEN", FAKE_TOKEN)
    with pytest.raises(JiraError, match="JIRA_BASE_URL and JIRA_PROJECT"):
        JiraAdapter(data_dir=tmp_path)
