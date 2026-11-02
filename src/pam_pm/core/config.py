"""App configuration: tier, spend cap, dry run, data dir."""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "pam-pm"
DEFAULT_DAILY_SPEND_CAP = 2.0

BACKEND_PAM_BOARD = "pam_board"
BACKEND_AZURE_DEVOPS = "azure_devops"
BACKEND_JIRA = "jira"
BACKEND_LINEAR = "linear"
BACKEND_TRELLO = "trello"
BACKEND_CLICKUP = "clickup"
BACKEND_ASANA = "asana"
BACKEND_MONDAY = "monday"
BACKEND_CHOICES = (
    BACKEND_PAM_BOARD,
    BACKEND_AZURE_DEVOPS,
    BACKEND_JIRA,
    BACKEND_LINEAR,
    BACKEND_TRELLO,
    BACKEND_CLICKUP,
    BACKEND_ASANA,
    BACKEND_MONDAY,
)

_PAM_ALIASES = {"pam", "pam_board", "native", "native_board", "sqlite", "sqlite_board"}
_ADO_ALIASES = {"ado", "azure", "azure_devops", "azure-devops", "devops"}
_JIRA_ALIASES = {"jira", "jira_cloud"}
_LINEAR_ALIASES = {"linear"}
_TRELLO_ALIASES = {"trello"}
_CLICKUP_ALIASES = {"clickup", "click_up"}
_ASANA_ALIASES = {"asana"}
_MONDAY_ALIASES = {"monday", "monday_com", "monday.com"}


def normalize_backend(value: object) -> str:
    """Return the canonical backend for a user or stored value.

    Unknown, empty, or missing values default to azure_devops so
    existing projects keep working with no user step.
    """
    text = str(value or "").strip().lower().replace("-", "_")
    if text in _PAM_ALIASES:
        return BACKEND_PAM_BOARD
    if text in _ADO_ALIASES or not text:
        return BACKEND_AZURE_DEVOPS
    if text in _JIRA_ALIASES:
        return BACKEND_JIRA
    if text in _LINEAR_ALIASES:
        return BACKEND_LINEAR
    if text in _TRELLO_ALIASES:
        return BACKEND_TRELLO
    if text in _CLICKUP_ALIASES:
        return BACKEND_CLICKUP
    if text in _ASANA_ALIASES:
        return BACKEND_ASANA
    if text in _MONDAY_ALIASES:
        return BACKEND_MONDAY
    if text in BACKEND_CHOICES:
        return text
    return BACKEND_AZURE_DEVOPS


def backend_label(value: object) -> str:
    """Return the display label for a backend value."""
    backend = normalize_backend(value)
    if backend == BACKEND_PAM_BOARD:
        return "Pam board"
    if backend == BACKEND_JIRA:
        return "Jira"
    if backend == BACKEND_LINEAR:
        return "Linear"
    if backend == BACKEND_TRELLO:
        return "Trello"
    if backend == BACKEND_CLICKUP:
        return "ClickUp"
    if backend == BACKEND_ASANA:
        return "Asana"
    if backend == BACKEND_MONDAY:
        return "Monday"
    return "Azure DevOps"


def resolve_data_dir(explicit: str | Path | None = None) -> Path:
    """Return the data dir, honoring PAM_HOME then platformdirs."""
    home = os.environ.get("PAM_HOME", "").strip()
    if explicit is not None:
        return Path(explicit).expanduser()
    if home:
        return Path(home).expanduser()
    return Path(user_data_dir(APP_NAME))


class Config:
    """Runtime settings loaded from args with env fallbacks.

    tier defaults to standard. Contributor tier is only for runs with
    fake data, enforced by require_fake_for_contributor.
    """

    def __init__(
        self,
        model: str | None = None,
        tier: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        max_tokens: int | None = None,
        daily_spend_cap: float | None = None,
        dry_run: bool | None = None,
        data_dir: str | Path | None = None,
        model_tier: str | None = None,
        backend: str | None = None,
        allowed_host: str | None = None,
        share_on_lan: bool | None = None,
        poll_interval: int | None = None,
    ) -> None:
        want_tier = model_tier or tier or os.environ.get("META_TIER", "")
        want_tier = (want_tier or "standard").strip().lower()
        if want_tier not in ("standard", "contributor"):
            want_tier = "standard"
        self.tier = want_tier
        env_home_early = os.environ.get("PAM_HOME", "").strip()
        if data_dir is not None:
            early_dir = Path(data_dir).expanduser()
        elif env_home_early:
            early_dir = Path(env_home_early).expanduser()
        else:
            early_dir = Path(user_data_dir(APP_NAME))
        saved_model = ""
        saved_base = ""
        saved_key = ""
        try:
            from pam_pm.core import secrets as secrets_mod

            saved = secrets_mod.load_meta_config(early_dir)
            saved_model = str(saved.get("model", "") or "")
            saved_base = str(saved.get("base_url", "") or "")
            saved_key = secrets_mod.load_secret("META_API_KEY", early_dir)
        except Exception:  # noqa: BLE001
            saved_model, saved_base, saved_key = "", "", ""
        self.model = (
            model or os.environ.get("META_MODEL", "") or saved_model
        ).strip()
        self.base_url = (
            base_url or os.environ.get("META_BASE_URL", "") or saved_base
        ).strip()
        self.api_key = (
            api_key or os.environ.get("META_API_KEY", "") or saved_key
        ).strip()
        env_tokens = os.environ.get("META_MAX_TOKENS", "").strip()
        if max_tokens is not None:
            self.max_tokens = int(max_tokens)
        elif env_tokens:
            try:
                self.max_tokens = int(env_tokens)
            except ValueError:
                self.max_tokens = 4000
        else:
            self.max_tokens = 4000
        env_cap = os.environ.get("DAILY_SPEND_CAP", "").strip()
        if daily_spend_cap is not None:
            self.daily_spend_cap = float(daily_spend_cap)
        elif env_cap:
            try:
                self.daily_spend_cap = float(env_cap)
            except ValueError:
                self.daily_spend_cap = DEFAULT_DAILY_SPEND_CAP
        else:
            self.daily_spend_cap = DEFAULT_DAILY_SPEND_CAP
        env_dry = os.environ.get("PAM_DRY_RUN", "").strip().lower()
        if dry_run is not None:
            self.dry_run = bool(dry_run)
        elif env_dry in ("0", "false", "no", "off"):
            self.dry_run = False
        elif env_dry in ("1", "true", "yes", "on"):
            self.dry_run = True
        else:
            self.dry_run = True
        env_home = os.environ.get("PAM_HOME", "").strip()
        if data_dir is not None:
            self.data_dir = Path(data_dir).expanduser()
        elif env_home:
            self.data_dir = Path(env_home).expanduser()
        else:
            self.data_dir = Path(user_data_dir(APP_NAME))
        want_backend = backend if backend is not None else os.environ.get("PAM_BACKEND", "")
        self.backend = normalize_backend(want_backend)
        if allowed_host is not None:
            self.allowed_host = str(allowed_host).strip()
        else:
            self.allowed_host = os.environ.get("PAM_ALLOWED_HOST", "").strip()
        env_share = os.environ.get("PAM_SHARE_ON_LAN", "").strip().lower()
        if share_on_lan is not None:
            self.share_on_lan = bool(share_on_lan)
        elif env_share in ("1", "true", "yes", "on"):
            self.share_on_lan = True
        elif env_share in ("0", "false", "no", "off"):
            self.share_on_lan = False
        else:
            self.share_on_lan = False
        env_poll = os.environ.get("PAM_POLL_INTERVAL", "").strip()
        if poll_interval is not None:
            try:
                self.poll_interval = max(1, int(poll_interval))
            except (TypeError, ValueError):
                self.poll_interval = 60
        elif env_poll:
            try:
                self.poll_interval = max(1, int(env_poll))
            except ValueError:
                self.poll_interval = 60
        else:
            self.poll_interval = 60

    @property
    def model_tier(self) -> str:
        return self.tier

    @model_tier.setter
    def model_tier(self, value: str) -> None:
        norm = (value or "standard").strip().lower()
        self.tier = norm if norm in ("standard", "contributor") else "standard"

    @property
    def meta_model(self) -> str:
        return self.model

    @property
    def meta_tier(self) -> str:
        return self.tier

    def require_fake_for_contributor(self, *, fake: bool = False) -> None:
        """Raise when contributor tier is used with non fake data."""
        if self.tier == "contributor" and not fake:
            raise ValueError("contributor tier is only for runs with fake data")

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir

    def db_path(self, name: str = "state.db") -> Path:
        return self.ensure_data_dir() / name

    def __repr__(self) -> str:
        return (
            f"Config(tier={self.tier!r}, model={self.model!r}, "
            f"daily_spend_cap={self.daily_spend_cap!r}, dry_run={self.dry_run!r}, "
            f"data_dir={str(self.data_dir)!r})"
        )


Settings = Config
