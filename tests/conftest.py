"""Hermetic tests: isolate PAM_HOME per test so no test touches the real DB."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_pam_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point PAM_HOME at a fresh tmp dir for every test."""
    home = tmp_path / "pam-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAM_HOME", str(home))
    old_cwd = Path.cwd()
    try:
        yield home
    finally:
        try:
            os.chdir(old_cwd)
        except OSError:
            pass
