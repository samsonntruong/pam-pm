"""Slack Socket Mode inbound: fast path beside the always-on poller.

Opens one outbound websocket with the saved app token (xapp), so live
inbound arrives with no inbound ports. Message payloads route to the
same handle_slack_event path the poller and webhook use, sharing the
same seen ids, so dedupe and resume behavior are identical.

Importing or constructing anything here never touches the network.
Only start() opens the websocket, in a daemon supervisor thread that
reconnects with backoff when the socket drops. Tests inject a fake
client factory.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
HEALTH_CHECK_SECONDS = 5.0


def backoff_delay(attempt: int) -> float:
    """Return the reconnect delay for a zero based attempt count.

    Exponential from 1s, capped at 60s: 1, 2, 4, 8, 16, 32, 60, 60...
    """
    index = max(0, int(attempt))
    return min(BACKOFF_MAX_SECONDS, BACKOFF_INITIAL_SECONDS * (2.0**index))


def socket_dedupe_id(payload: dict[str, Any]) -> str:
    """Return the poller style dedupe id for a socket payload, or blank.

    Socket Mode delivers the same Events API envelope shape the
    webhook route receives, so the shared id is slack:channel:ts.
    """
    envelope = payload if isinstance(payload, dict) else {}
    inner = envelope.get("event")
    if not isinstance(inner, dict):
        inner = envelope
    if str(inner.get("type", "") or "") != "message":
        return ""
    channel = str(inner.get("channel", "") or "").strip()
    ts = str(inner.get("ts", "") or inner.get("event_ts", "") or "").strip()
    if not channel or not ts:
        return ""
    return f"slack:{channel}:{ts}"


def _request_parts(request: Any) -> tuple[str, str, Any]:
    """Return type, envelope id, and payload from a socket request.

    Accepts a slack_sdk SocketModeRequest or a plain dict with the
    same keys, so fakes stay trivial.
    """
    if isinstance(request, dict):
        return (
            str(request.get("type", "") or ""),
            str(request.get("envelope_id", "") or ""),
            request.get("payload"),
        )
    return (
        str(getattr(request, "type", "") or ""),
        str(getattr(request, "envelope_id", "") or ""),
        getattr(request, "payload", None),
    )


def default_client_factory(app_token: str, web_client: Any = None) -> Any:
    """Build a real slack_sdk SocketModeClient without connecting.

    Construction never touches the network. Raises SlackError shaped
    RuntimeError when slack-sdk is missing.
    """
    try:
        from slack_sdk.socket_mode import SocketModeClient
    except ImportError as exc:
        raise RuntimeError(f"slack-sdk is not installed: {exc}") from exc
    kwargs: dict[str, Any] = {"app_token": app_token}
    if web_client is not None:
        kwargs["web_client"] = web_client
    return SocketModeClient(**kwargs)


class SocketModeInbound:
    """Own one Socket Mode websocket with reconnect and ack.

    on_envelope receives each events_api payload dict. on_status
    receives True when the socket goes live and False when it drops.
    Every request carrying an envelope id is acked, even when the
    payload type is not handled.
    """

    def __init__(
        self,
        app_token: str,
        on_envelope: Callable[[dict[str, Any]], Any],
        *,
        client_factory: Callable[..., Any] | None = None,
        on_status: Callable[[bool], None] | None = None,
        web_client: Any = None,
        sleep: Callable[[float], None] | None = None,
        health_interval: float = HEALTH_CHECK_SECONDS,
    ) -> None:
        self._app_token = app_token
        self._on_envelope = on_envelope
        self._client_factory = client_factory or default_client_factory
        self._on_status = on_status
        self._web_client = web_client
        self._sleep = sleep or time.sleep
        self._health_interval = max(0.001, float(health_interval))
        self._stopped = threading.Event()
        self._live = False
        self._lock = threading.Lock()
        self._client: Any = None
        self._thread: threading.Thread | None = None

    @property
    def is_healthy(self) -> bool:
        """Return True while the socket is connected and live."""
        with self._lock:
            return self._live

    def _set_live(self, value: bool) -> None:
        changed = False
        with self._lock:
            if value != self._live:
                self._live = value
                changed = True
        if changed and self._on_status is not None:
            try:
                self._on_status(value)
            except Exception:  # noqa: BLE001
                logger.info("slack socket status callback failed")

    def handle_request(self, client: Any, request: Any) -> dict[str, Any]:
        """Route one socket request to on_envelope, then ack it.

        Returns the events_api payload dict, or {} for other types.
        """
        req_type, envelope_id, payload = _request_parts(request)
        logger.info(
            "slack socket envelope received: type=%s", req_type or "unknown"
        )
        out: dict[str, Any] = {}
        if req_type in ("events_api", "interactive") and isinstance(payload, dict):
            out = payload
            try:
                self._on_envelope(payload)
            except Exception:  # noqa: BLE001
                logger.info("slack socket envelope failed, keeping socket up")
        if envelope_id and client is not None:
            try:
                client.send_socket_mode_response({"envelope_id": envelope_id})
            except Exception:  # noqa: BLE001
                logger.info("slack socket ack failed, keeping socket up")
        return out

    @staticmethod
    def _client_connected(client: Any) -> bool:
        check = getattr(client, "is_connected", None)
        if not callable(check):
            return False
        try:
            return bool(check())
        except Exception:  # noqa: BLE001
            return False

    def _supervise(self) -> None:
        attempt = 0
        while not self._stopped.is_set():
            client: Any = None
            try:
                client = self._client_factory(self._app_token, self._web_client)
                listeners = getattr(client, "socket_mode_request_listeners", None)
                if isinstance(listeners, list):
                    listeners.append(self.handle_request)
                client.connect()
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "slack socket connect failed: attempt=%d reason=%s",
                    attempt,
                    exc,
                )
                self._sleep(backoff_delay(attempt))
                attempt += 1
                continue
            with self._lock:
                self._client = client
            attempt = 0
            self._set_live(True)
            try:
                while not self._stopped.is_set() and self._client_connected(client):
                    self._sleep(self._health_interval)
            finally:
                self._set_live(False)
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    logger.info("slack socket close skipped")
                with self._lock:
                    if self._client is client:
                        self._client = None
            if self._stopped.is_set():
                break
            logger.info(
                "slack socket dropped: attempt=%d reason=connection lost, "
                "reconnecting",
                attempt,
            )
            self._sleep(backoff_delay(attempt))
            attempt += 1

    def start(self) -> bool:
        """Start the supervisor thread, connecting in the background."""
        self._stopped.clear()
        thread = self._thread
        if thread is not None and thread.is_alive():
            return True
        worker = threading.Thread(
            target=self._supervise, name="slack-socket", daemon=True
        )
        self._thread = worker
        worker.start()
        return True

    def stop(self) -> None:
        """Stop the supervisor and close the socket."""
        self._stopped.set()
        with self._lock:
            client = self._client
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                logger.info("slack socket close skipped")
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._thread = None
        self._set_live(False)

    def wait_until_live(self, timeout: float = 5.0) -> bool:
        """Block until the socket is live, returning False on timeout."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if self.is_healthy:
                return True
            time.sleep(0.01)
        return self.is_healthy
