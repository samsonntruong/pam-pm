"""Entra device code flow for Teams Graph polling.

Pam reads Teams 1:1 chats and channels through Microsoft Graph with a
delegated user sign-in, so no public endpoint and no inbound ports are
needed. The wizard starts a device code, shows the user code plus the
microsoft.com/devicelogin link, then polls for tokens. The refresh
token is stored in the secrets store (0600) and never logged; access
tokens are minted silently from it and cached until near expiry
(Graph tokens live about one hour).

The client secret is only attached to token calls when one is
configured; public-client style polling without a secret works too.
Importing or constructing anything here never touches the network.
Tests use httpx.MockTransport.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEVICE_CODE_URL_TEMPLATE = (
    "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"
)
TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
DEVICELOGIN_URL = "https://microsoft.com/devicelogin"
GRAPH_DEVICE_SCOPES = "Chat.Read Chat.ReadWrite ChannelMessage.Read.All offline_access"

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

REFRESH_SECRET_NAME = "TEAMS_REFRESH_TOKEN"

_EXPIRY_SKEW_SECONDS = 300


def _redact(text: object) -> str:
    try:
        from pam_pm.adapters.chat.teams import redact as teams_redact
    except Exception:  # noqa: BLE001
        return "[REDACTED]"
    try:
        return teams_redact(text)
    except Exception:  # noqa: BLE001
        return "[REDACTED]"


class DeviceAuthError(RuntimeError):
    """Device code or refresh failure with secrets already redacted."""


class DeviceCodePending(DeviceAuthError):
    """The user has not finished the device code prompt yet."""


class DeviceCodeSlowDown(DeviceAuthError):
    """Polling too fast; the caller should wait longer before retrying."""


class DeviceCodeExpired(DeviceAuthError):
    """The device code expired before the user signed in."""


@dataclass
class DeviceCodeSession:
    """One in-progress device code sign-in."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    message: str
    app_id: str
    tenant_id: str
    scopes: str = GRAPH_DEVICE_SCOPES


@dataclass
class DeviceTokenResult:
    """Tokens from a completed device code or refresh call."""

    access_token: str
    refresh_token: str
    expires_in: int


def _fail(op: str, exc: Exception) -> DeviceAuthError:
    detail = _redact(exc)
    logger.error("teams device %s failed: %s", op, detail)
    return DeviceAuthError(f"{op} failed: {detail}")


def start_device_code(
    app_id: str,
    tenant_id: str,
    scopes: str = GRAPH_DEVICE_SCOPES,
    timeout: int = 30,
    transport: httpx.BaseTransport | None = None,
) -> DeviceCodeSession:
    """Start a device code sign-in, returning codes for the wizard to show."""
    client_id = (app_id or "").strip()
    tenant = (tenant_id or "").strip()
    if not client_id or not tenant:
        raise DeviceAuthError(
            "TEAMS_APP_ID and TEAMS_TENANT_ID are required; save them in Setup first."
        )
    url = DEVICE_CODE_URL_TEMPLATE.format(tenant=tenant)
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            resp = client.post(
                url, data={"client_id": client_id, "scope": scopes or GRAPH_DEVICE_SCOPES}
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        raise _fail("start", exc) from exc
    if not isinstance(payload, dict) or not payload.get("device_code"):
        raise DeviceAuthError("device code endpoint returned no device code.")
    try:
        expires_in = int(payload.get("expires_in", 900) or 900)
    except (TypeError, ValueError):
        expires_in = 900
    try:
        interval = int(payload.get("interval", 5) or 5)
    except (TypeError, ValueError):
        interval = 5
    logger.info("teams device code started: user_code shown in wizard")
    return DeviceCodeSession(
        device_code=str(payload.get("device_code", "") or ""),
        user_code=str(payload.get("user_code", "") or ""),
        verification_uri=str(payload.get("verification_uri", "") or "") or DEVICELOGIN_URL,
        expires_in=max(1, expires_in),
        interval=max(1, interval),
        message=str(payload.get("message", "") or ""),
        app_id=client_id,
        tenant_id=tenant,
        scopes=scopes or GRAPH_DEVICE_SCOPES,
    )


def _token_payload_error(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("error", "") or "")
    return ""


def poll_device_token(
    session: DeviceCodeSession,
    client_secret: str = "",
    timeout: int = 30,
    transport: httpx.BaseTransport | None = None,
) -> DeviceTokenResult:
    """Poll once for device code completion.

    Raises DeviceCodePending or DeviceCodeSlowDown while the user has
    not finished, DeviceCodeExpired when the code lapsed, and returns
    tokens once the user approves the sign-in.
    """
    url = TOKEN_URL_TEMPLATE.format(tenant=session.tenant_id)
    form: dict[str, str] = {
        "grant_type": DEVICE_GRANT,
        "client_id": session.app_id,
        "device_code": session.device_code,
    }
    if (client_secret or "").strip():
        form["client_secret"] = client_secret.strip()
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            resp = client.post(url, data=form)
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            if resp.status_code == 200:
                access = str(payload.get("access_token", "") or "")
                refresh = str(payload.get("refresh_token", "") or "")
                if not access or not refresh:
                    raise DeviceAuthError(
                        "token endpoint returned no access or refresh token."
                    )
                try:
                    expires = int(payload.get("expires_in", 3600) or 3600)
                except (TypeError, ValueError):
                    expires = 3600
                logger.info("teams device sign-in complete")
                return DeviceTokenResult(
                    access_token=access, refresh_token=refresh, expires_in=max(1, expires)
                )
            code = _token_payload_error(payload)
            if code == "authorization_pending":
                raise DeviceCodePending("waiting for the user to sign in.")
            if code == "slow_down":
                raise DeviceCodeSlowDown("polling too fast; waiting longer.")
            if code in ("expired_token", "code_expired"):
                raise DeviceCodeExpired("device code expired; start sign-in again.")
            resp.raise_for_status()
            raise DeviceAuthError(f"token endpoint returned {resp.status_code}.")
    except (DeviceCodePending, DeviceCodeSlowDown, DeviceCodeExpired, DeviceAuthError):
        raise
    except Exception as exc:
        raise _fail("poll", exc) from exc


def refresh_access_token(
    app_id: str,
    tenant_id: str,
    refresh_token: str,
    client_secret: str = "",
    scopes: str = GRAPH_DEVICE_SCOPES,
    timeout: int = 30,
    transport: httpx.BaseTransport | None = None,
) -> DeviceTokenResult:
    """Mint a fresh access token from a stored refresh token."""
    client_id = (app_id or "").strip()
    tenant = (tenant_id or "").strip()
    refresh = (refresh_token or "").strip()
    if not client_id or not tenant or not refresh:
        raise DeviceAuthError("app ID, tenant ID, and a refresh token are required.")
    url = TOKEN_URL_TEMPLATE.format(tenant=tenant)
    form: dict[str, str] = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh,
        "scope": scopes or GRAPH_DEVICE_SCOPES,
    }
    if (client_secret or "").strip():
        form["client_secret"] = client_secret.strip()
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            resp = client.post(url, data=form)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        raise _fail("refresh", exc) from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise DeviceAuthError("refresh endpoint returned no access token.")
    try:
        expires = int(payload.get("expires_in", 3600) or 3600)
    except (TypeError, ValueError):
        expires = 3600
    return DeviceTokenResult(
        access_token=str(payload.get("access_token", "") or ""),
        refresh_token=str(payload.get("refresh_token", "") or "") or refresh,
        expires_in=max(1, expires),
    )


def store_teams_refresh_token(
    value: str, data_dir: str | Path | None = None
) -> bool:
    """Store the Teams refresh token (0600 file, never logged)."""
    from pam_pm.core import secrets as secrets_mod

    return secrets_mod.save_secret(REFRESH_SECRET_NAME, value, data_dir)


def load_teams_refresh_token(data_dir: str | Path | None = None) -> str:
    """Return the stored Teams refresh token, or empty when missing."""
    from pam_pm.core import secrets as secrets_mod

    return secrets_mod.load_secret(REFRESH_SECRET_NAME, data_dir)


class GraphTokenProvider:
    """Silent hourly access token minting from the stored refresh token.

    The first call refreshes from the secrets store; later calls reuse
    the cached token until near expiry, then mint again with no user
    step. Rotated refresh tokens are stored back immediately.
    """

    def __init__(
        self,
        app_id: str = "",
        tenant_id: str = "",
        client_secret: str = "",
        scopes: str = GRAPH_DEVICE_SCOPES,
        data_dir: str | Path | None = None,
        timeout: int = 30,
        transport: httpx.BaseTransport | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._app_id = (app_id or "").strip()
        self._tenant_id = (tenant_id or "").strip()
        self._client_secret = (client_secret or "").strip()
        self._scopes = scopes or GRAPH_DEVICE_SCOPES
        self._data_dir = data_dir
        self._timeout = timeout
        self._transport = transport
        self._now = time_fn or time.time
        self._token = ""
        self._expires_at = 0.0

    def _resolve_config(self) -> None:
        if self._app_id and self._tenant_id:
            return
        try:
            from pam_pm.core import secrets as secrets_mod

            effective = secrets_mod.effective_teams_config(self._data_dir)
        except Exception:  # noqa: BLE001
            effective = {}
        self._app_id = self._app_id or str(effective.get("app_id", "") or "")
        self._tenant_id = self._tenant_id or str(effective.get("tenant_id", "") or "")
        self._client_secret = self._client_secret or str(
            effective.get("app_password", "") or ""
        )

    def access_token(self) -> str:
        """Return a valid Graph access token, minting when needed."""
        if self._token and self._now() < self._expires_at:
            return self._token
        self._resolve_config()
        refresh = load_teams_refresh_token(self._data_dir)
        if not refresh:
            raise DeviceAuthError(
                "Teams sign-in is missing; run the device code step in Setup."
            )
        result = refresh_access_token(
            self._app_id,
            self._tenant_id,
            refresh,
            client_secret=self._client_secret,
            scopes=self._scopes,
            timeout=self._timeout,
            transport=self._transport,
        )
        if result.refresh_token != refresh:
            store_teams_refresh_token(result.refresh_token, self._data_dir)
        self._token = result.access_token
        self._expires_at = self._now() + max(1, result.expires_in - _EXPIRY_SKEW_SECONDS)
        logger.info("teams graph token minted silently")
        return self._token

    def clear_cache(self) -> None:
        """Drop the cached access token so the next call mints again."""
        self._token = ""
        self._expires_at = 0.0
