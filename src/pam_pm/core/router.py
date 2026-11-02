"""Central model routing: one place for every model ID.

All Meta model selection lives here. Slack text, interviews, vision reads,
review and retro synthesis, next sprint proposals, and listen notes resolve
to META_MODEL (Muse Spark text). Sticky note segmentation resolves to
META_VISION_MODEL (SAM 3.1). Realtime transcription resolves to the Muse
Voice Transcribe model. All paths share the one META_API_KEY and there is
no user choice of model.

Every model call logs capability plus model used through log_route. Planner
runs log which path was used (SAM path or fallback path) so Activity can
show it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

CAP_SLACK_TEXT = "slack_text"
CAP_INTERVIEW = "interview"
CAP_VISION_SEGMENT = "vision_segment"
CAP_VISION_READ = "vision_read"
CAP_PLAN = "plan"
CAP_REVIEW = "review"
CAP_RETRO = "retro"
CAP_NEXT_SPRINT = "next_sprint"
CAP_LISTEN_NOTES = "listen_notes"
CAP_TRANSCRIBE_REALTIME = "transcribe_realtime"
CAP_SLACK_AUDIO = "slack_audio"

DEFAULT_VISION_MODEL = "sam-3.1"
TRANSCRIBE_MODEL = "muse-voice-transcribe-1.0"

TEXT_CAPABILITIES = frozenset(
    {
        CAP_SLACK_TEXT,
        CAP_INTERVIEW,
        CAP_VISION_READ,
        CAP_PLAN,
        CAP_REVIEW,
        CAP_RETRO,
        CAP_NEXT_SPRINT,
        CAP_LISTEN_NOTES,
    }
)

IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".bmp"}
)
AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".wav", ".m4a", ".ogg", ".opus", ".flac", ".aac", ".wma", ".aiff"}
)

SLACK_AUDIO_REPLY = (
    "I cannot listen to audio in Slack yet. "
    "Please open the Listen page in the Pam console and use the Listen "
    "button to capture this meeting live."
)


def text_model(explicit: str | None = None) -> str:
    """Return the Muse Spark text model ID from config or META_MODEL."""
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    return os.environ.get("META_MODEL", "").strip()


def vision_segment_model(explicit: str | None = None) -> str:
    """Return the SAM model ID from META_VISION_MODEL, default sam-3.1."""
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    return os.environ.get("META_VISION_MODEL", "").strip() or DEFAULT_VISION_MODEL


def transcribe_model() -> str:
    """Return the Muse Voice Transcribe model ID."""
    return TRANSCRIBE_MODEL


def resolve(capability: str, provider: str = "meta") -> dict[str, str]:
    """Resolve a capability to its model ID, provider, and capability name."""
    cap = str(capability or "").strip() or CAP_SLACK_TEXT
    want = str(provider or "meta").strip().lower() or "meta"
    if cap == CAP_VISION_SEGMENT:
        model = vision_segment_model() if want == "meta" else ""
    elif cap == CAP_TRANSCRIBE_REALTIME:
        model = transcribe_model() if want == "meta" else ""
    elif cap == CAP_SLACK_AUDIO:
        model = ""
    else:
        model = text_model()
    return {"capability": cap, "model": model, "provider": want}


def log_route(
    capability: str,
    model: str | None = None,
    extra: str = "",
    provider: str = "meta",
) -> dict[str, str]:
    """Log capability plus model used, returning the route record."""
    route = resolve(capability, provider)
    used = str(model).strip() if model is not None else route["model"]
    detail = f"model route: capability={route['capability']} model={used or 'none'}"
    if extra:
        detail += f" {extra}"
    logger.info(detail)
    return {"capability": route["capability"], "model": used, "provider": route["provider"]}


def _extension_of(filename: str) -> str:
    name = str(filename or "").strip().lower()
    if "." not in name:
        return ""
    return name[name.rfind(".") :]


def is_image_file(mimetype: str = "", filename: str = "") -> bool:
    """Return True for Slack image uploads by mimetype or extension."""
    mime = str(mimetype or "").strip().lower()
    if mime.startswith("image/"):
        return True
    return _extension_of(filename) in IMAGE_EXTENSIONS


def is_audio_file(mimetype: str = "", filename: str = "") -> bool:
    """Return True for Slack audio uploads by mimetype or extension."""
    mime = str(mimetype or "").strip().lower()
    if mime.startswith("audio/"):
        return True
    if mime in ("video/mp4", "video/mpeg"):
        return False
    return _extension_of(filename) in AUDIO_EXTENSIONS


def decline_slack_audio(chat: Any, channel: str) -> dict[str, Any]:
    """Reply once that Slack audio is not transcribed, pointing to Listen."""
    log_route(CAP_SLACK_AUDIO, "", "audio in Slack is not transcribed")
    message_id = chat.send_dm(channel, SLACK_AUDIO_REPLY)
    return {
        "kind": "audio_declined",
        "capability": CAP_SLACK_AUDIO,
        "model": "",
        "replied": True,
        "message_id": str(message_id),
    }


def handle_slack_file(
    *,
    file_id: str,
    channel: str,
    chat: Any,
    board: Any = None,
    mimetype: str = "",
    filename: str = "",
    file_bytes: bytes | None = None,
    vision: Any = None,
    segmenter: Any = None,
    provider: str = "meta",
    event_sink: Any = None,
) -> dict[str, Any]:
    """Route one Slack file: image to planner, audio to polite decline.

    Images in a Slack DM or thread go to the sticky note planner: SAM
    finds notes, Muse Spark reads crops, and the proposal posts with an
    Approve button. A SAM runtime failure falls back to whole image
    Muse Spark automatically, logging which path was used. Audio files
    are never transcribed: Pam replies once politely and points to the
    Listen page. Unknown file kinds are ignored without reply.
    """
    want = str(provider or "meta").strip().lower() or "meta"
    if is_audio_file(mimetype, filename):
        return decline_slack_audio(chat, channel)
    if not is_image_file(mimetype, filename):
        logger.info("slack file ignored: file_id=%s mimetype=%s", file_id, mimetype)
        return {"kind": "ignored", "replied": False}
    log_route(CAP_VISION_SEGMENT, vision_segment_model(), f"file_id={file_id}")
    data = bytes(file_bytes) if file_bytes is not None else bytes(chat.download_file(file_id))
    from pam_pm.agents.planner import propose_sprint
    from pam_pm.models.vision import plan_from_photo

    photo = plan_from_photo(data, vision=vision, segmenter=segmenter, provider=want)
    path = str(photo.get("path", "fallback"))
    readings = photo.get("readings", [])
    log_route(CAP_VISION_READ, text_model(), f"planner path={path}")
    proposal = propose_sprint(readings or photo.get("notes_text", ""))
    if board is not None:
        from pam_pm.agents.planner import approve_proposal as _approve

        _ = _approve
    lines = [f"Found {len(readings)} notes in the photo (path: {path})."]
    for entry in proposal[:10]:
        lines.append(f"- {entry.get('title', '')}")
    message_id = chat.send_buttons(
        channel,
        "\n".join(lines),
        [
            {"id": "plan_approve", "label": "Approve", "value": "approve"},
            {"id": "plan_cancel", "label": "Cancel", "value": "cancel"},
        ],
    )
    event = {
        "operation": "plan_from_photo",
        "item_id": "",
        "evidence_quote": f"Planner path: {path} from Slack file {file_id}.",
        "status": "proposed",
        "detail": f"path={path} readings={len(readings)} proposal={len(proposal)}",
    }
    if event_sink is not None:
        try:
            event_sink(event)
        except Exception:  # noqa: BLE001
            logger.info("planner event sink failed, keeping local result")
    logger.info(
        "vision path used: %s (Slack file %s, %d readings)",
        path,
        file_id,
        len(readings),
    )
    return {
        "kind": "image_plan",
        "capability": CAP_PLAN,
        "model": text_model(),
        "path": path,
        "readings": readings,
        "proposal": proposal,
        "message_id": str(message_id),
        "event": event,
    }


def _event_file_meta(file_obj: Any) -> tuple[str, str]:
    if not isinstance(file_obj, dict):
        return "", ""
    mime = str(file_obj.get("mimetype", "") or "")
    name = str(file_obj.get("name", "") or file_obj.get("title", "") or "")
    return mime, name


def _event_channel_fallback(file_obj: Any) -> str:
    if not isinstance(file_obj, dict):
        return ""
    for key in ("channels", "groups", "ims"):
        raw = file_obj.get(key)
        if isinstance(raw, list) and raw:
            return str(raw[0])
    shares = file_obj.get("shares")
    if isinstance(shares, dict):
        for raw in shares.values():
            if isinstance(raw, list) and raw:
                first = raw[0]
                if isinstance(first, dict):
                    ident = str(first.get("id", "") or "")
                else:
                    ident = str(first or "")
                if ident:
                    return ident
    return ""


def handle_slack_event(
    event: Any,
    *,
    chat: Any,
    board: Any = None,
    mimetype: str = "",
    filename: str = "",
    file_bytes: bytes | None = None,
    vision: Any = None,
    segmenter: Any = None,
    provider: str = "meta",
    event_sink: Any = None,
) -> dict[str, Any]:
    """Route a Slack event: file shares flow to the planner automatically.

    Accepts a full Events API envelope or the inner event. A file_shared
    event for an image in a DM or in a thread Pam is in goes straight to
    handle_slack_file: SAM plus Muse Spark read the notes and the
    proposal posts back to the same channel with an Approve button, so no
    manual upload is needed. Message events carrying files route the
    same way. Metadata comes from the event inline, else from
    chat.file_info, else the file is ignored. Unknown events, files
    without an id or channel, and non file events are ignored without a
    reply. Audio files get the one polite Listen reply from
    handle_slack_file.
    """
    payload = event if isinstance(event, dict) else {}
    if str(payload.get("type", "") or "") == "url_verification":
        return {
            "kind": "url_verification",
            "challenge": str(payload.get("challenge", "") or ""),
            "replied": False,
        }
    inner = payload.get("event")
    if not isinstance(inner, dict):
        inner = payload
    kind = str(inner.get("type", "") or "")
    file_id = ""
    channel = ""
    mime = str(mimetype or "")
    name = str(filename or "")
    if kind == "file_shared":
        file_id = str(inner.get("file_id", "") or "")
        channel = str(inner.get("channel_id", "") or "")
        file_obj = inner.get("file")
        if isinstance(file_obj, dict):
            file_id = file_id or str(file_obj.get("id", "") or "")
            inline_mime, inline_name = _event_file_meta(file_obj)
            mime = mime or inline_mime
            name = name or inline_name
            channel = channel or _event_channel_fallback(file_obj)
        if not channel:
            channel = str(inner.get("user_id", "") or "")
    elif kind == "message":
        files = inner.get("files")
        first: Any = files[0] if isinstance(files, list) and files else None
        if isinstance(first, dict):
            file_id = str(first.get("id", "") or "")
            inline_mime, inline_name = _event_file_meta(first)
            mime = mime or inline_mime
            name = name or inline_name
        channel = str(inner.get("channel", "") or "")
        if not channel:
            channel = str(inner.get("user", "") or "")
        if not file_id:
            logger.info("slack event ignored: message without files")
            return {"kind": "ignored", "replied": False}
    else:
        logger.info("slack event ignored: type=%s", kind or "none")
        return {"kind": "ignored", "replied": False}
    if not file_id or not channel:
        logger.info("slack event ignored: missing file id or channel")
        return {"kind": "ignored", "replied": False}
    if not mime and not name and chat is not None:
        info_fn = getattr(chat, "file_info", None)
        info: Any = {}
        if callable(info_fn):
            try:
                info = info_fn(file_id) or {}
            except Exception:  # noqa: BLE001
                info = {}
        if isinstance(info, dict):
            mime = str(info.get("mimetype", "") or "")
            name = name or str(info.get("filename", "") or info.get("name", "") or "")
    result = handle_slack_file(
        file_id=file_id,
        channel=channel,
        chat=chat,
        board=board,
        mimetype=mime,
        filename=name,
        file_bytes=file_bytes,
        vision=vision,
        segmenter=segmenter,
        provider=provider,
        event_sink=event_sink,
    )
    result["file_id"] = file_id
    result["channel"] = channel
    return result
