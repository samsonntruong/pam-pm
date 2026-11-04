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
CAP_TEAMS_TEXT = "teams_text"

DEFAULT_VISION_MODEL = "sam-3.1"
TRANSCRIBE_MODEL = "muse-voice-transcribe-1.0"

TEXT_CAPABILITIES = frozenset(
    {
        CAP_SLACK_TEXT,
        CAP_TEAMS_TEXT,
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
    caption: str = "",
    model_client: Any = None,
    db: Any = None,
    project: str = "default",
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
    from pam_pm.models.vision import VisionClient, plan_from_photo

    if vision is None and model_client is not None:
        vision = VisionClient(model_client=model_client, provider=want)
    photo = plan_from_photo(data, vision=vision, segmenter=segmenter, provider=want)
    path = str(photo.get("path", "fallback"))
    readings = photo.get("readings", [])
    log_route(CAP_VISION_READ, text_model(), f"planner path={path}")
    proposal = propose_sprint(readings or photo.get("notes_text", ""))
    proposal_id = ""
    if db is not None and proposal:
        try:
            from pam_pm.core import store as store_mod

            proposal_id = store_mod.save_plan_proposal(
                db, proposal, project=project, channel=channel
            )
        except Exception:  # noqa: BLE001
            proposal_id = ""
            logger.info("plan proposal store failed, buttons carry no id")
    approve_value = f"approve:{proposal_id}" if proposal_id else "approve"
    cancel_value = f"cancel:{proposal_id}" if proposal_id else "cancel"
    lines = [f"Found {len(readings)} notes in the photo (path: {path})."]
    context = str(caption or "").strip()
    if context:
        lines.append(f"Context: {context[:200]}")
    for entry in proposal[:10]:
        lines.append(f"- {entry.get('title', '')}")
    message_id = chat.send_buttons(
        channel,
        "\n".join(lines),
        [
            {"id": "plan_approve", "label": "Approve", "value": approve_value},
            {"id": "plan_cancel", "label": "Cancel", "value": cancel_value},
        ],
    )
    detail = f"path={path} readings={len(readings)} proposal={len(proposal)}"
    context = str(caption or "").strip()
    if context:
        detail += f" caption={context[:120]}"
    event = {
        "operation": "plan_from_photo",
        "item_id": "",
        "evidence_quote": f"Planner path: {path} from Slack file {file_id}.",
        "status": "proposed",
        "detail": detail,
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
        "proposal_id": proposal_id,
        "message_id": str(message_id),
        "event": event,
        "caption": str(caption or ""),
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
    db: Any = None,
    model_client: Any = None,
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
    if str(payload.get("type", "") or "") == "block_actions":
        return handle_slack_interaction(
            payload, chat=chat, board=board, db=db, event_sink=event_sink
        )
    inner = payload.get("event")
    if not isinstance(inner, dict):
        inner = payload
    kind = str(inner.get("type", "") or "")
    file_id = ""
    channel = ""
    mime = str(mimetype or "")
    name = str(filename or "")
    caption = ""
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
        caption = str(inner.get("text", "") or "")
        if not file_id:
            return _handle_slack_text_message(
                payload,
                inner,
                chat=chat,
                board=board,
                db=db,
                model_client=model_client,
            )
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
        caption=caption,
        model_client=model_client,
        db=db,
    )
    result["file_id"] = file_id
    result["channel"] = channel
    return result


PLAN_APPROVE_IDS = frozenset({"plan_approve", "approve"})
PLAN_CANCEL_IDS = frozenset({"plan_cancel", "cancel"})
OWNER_ACCEPT_IDS = frozenset({"accept", "listen_accept", "owner_accept"})
OWNER_FLAG_IDS = frozenset({"flag", "listen_flag", "owner_flag"})

UNKNOWN_PROPOSAL_REPLY = (
    "Sorry, I could not find that proposal. It may have expired or "
    "been handled already. Please send the photo again for a fresh proposal."
)


def _parse_action_value(value: str) -> tuple[str, str]:
    """Split a button value into (verb, proposal_id)."""
    text = str(value or "").strip()
    if ":" in text:
        verb, pid = text.split(":", 1)
        return verb.strip().lower(), pid.strip()
    return text.lower(), ""


def _owner_board_states(board: Any) -> list[str]:
    try:
        states = [str(s) for s in board.list_states() if str(s)]
    except Exception:  # noqa: BLE001
        states = []
    return states


def _lead_target(db: Any) -> str:
    """Return the lead review target: channel env, else lead DM."""
    try:
        from pam_pm.agents import lead as lead_mod

        channel = os.environ.get(lead_mod.REVIEW_CHANNEL_ENV, "").strip()
    except Exception:  # noqa: BLE001
        return ""
    if channel:
        return channel
    if db is None:
        return ""
    try:
        from pam_pm.core import memory as memory_mod

        for person in memory_mod.list_people(db):
            if not isinstance(person, dict):
                continue
            if "lead" in str(person.get("role", "")).lower():
                name = str(person.get("name", "")).strip()
                if name:
                    return name
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _handle_owner_action(
    chat: Any,
    board: Any,
    db: Any,
    event_sink: Any,
    target: str,
    action_id: str,
    item_id: str,
    *,
    flagged: bool,
) -> dict[str, Any]:
    """Apply an owner Accept or Flag click on a board item.

    Accept confirms the item by moving it to the active state and
    replies confirmed. Flag reopens it with a needs-review tag, DMs
    the lead for review, and replies confirmed. Never raises.
    """
    label = str(item_id or "").strip() or "unknown item"
    verb_name = str(action_id or "").strip() or ("flag" if flagged else "accept")
    if board is not None and label != "unknown item":
        states = _owner_board_states(board)
        try:
            if flagged:
                fields: dict[str, Any] = {}
                if states:
                    fields["state"] = states[0]
                tags = ""
                try:
                    current = board.get_item(label)
                    raw = dict(getattr(current, "fields", {}) or {})
                    tags = str(raw.get("tags", "") or "")
                except Exception:  # noqa: BLE001, S110
                    pass
                have = {p.strip().lower() for p in tags.replace(",", ";").split(";")}
                if "needs-review" not in have:
                    if tags.strip():
                        tags = f"{tags}; needs-review"
                    else:
                        tags = "needs-review"
                fields["tags"] = tags
                board.update_item(label, fields)
            elif states:
                picked = states[1] if len(states) > 1 else states[0]
                board.update_item(label, {"state": picked})
        except Exception:  # noqa: BLE001
            logger.info("owner action board update failed for %s", label)
    if flagged and chat is not None:
        lead = _lead_target(db)
        if lead:
            try:
                chat.send_dm(
                    lead, f"Item {label} was flagged and needs lead review."
                )
            except Exception:  # noqa: BLE001
                logger.info("owner flag lead DM failed for %s", label)
    if chat is not None and target:
        try:
            if flagged:
                chat.send_dm(target, f"Flagged. Item {label} is back in lead review.")
            else:
                chat.send_dm(target, f"Accepted. Item {label} confirmed.")
        except Exception:  # noqa: BLE001
            logger.info("owner action reply failed for %s", label)
    if event_sink is not None:
        try:
            event_sink(
                {
                    "operation": "owner_flag" if flagged else "owner_accept",
                    "item_id": label,
                    "evidence_quote": f"Owner clicked {verb_name} on item {label}.",
                    "status": "applied",
                    "detail": f"owner {verb_name} on item {label}",
                }
            )
        except Exception:  # noqa: BLE001
            logger.info("owner action event sink failed")
    return {
        "action": action_id,
        "kind": "flagged" if flagged else "accepted",
        "item_id": label,
    }


def handle_slack_interaction(
    payload: dict[str, Any],
    *,
    chat: Any,
    board: Any = None,
    db: Any = None,
    event_sink: Any = None,
) -> dict[str, Any]:
    """Apply or discard a pending plan proposal from a block_actions click.

    Approve creates the items through the normal approve_proposal write
    path and confirms in the DM. Cancel discards the proposal and
    confirms. Unknown or expired proposal ids get a polite reply.
    """
    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions:
        logger.info("slack interaction ignored: no actions")
        return {"kind": "ignored", "replied": False}
    user_obj = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    channel_obj = (
        payload.get("channel") if isinstance(payload.get("channel"), dict) else {}
    )
    target = str(user_obj.get("id", "") or "").strip() or str(
        channel_obj.get("id", "") or ""
    ).strip()
    outcomes: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_id = str(action.get("action_id", "") or "").strip()
        verb, proposal_id = _parse_action_value(action.get("value", ""))
        is_approve = action_id in PLAN_APPROVE_IDS or verb == "approve"
        is_cancel = action_id in PLAN_CANCEL_IDS or verb == "cancel"
        is_accept = action_id in OWNER_ACCEPT_IDS or verb == "accept"
        is_flag = action_id in OWNER_FLAG_IDS or verb == "flag"
        if is_accept or is_flag:
            raw_value = str(action.get("value", "") or "").strip()
            item_id = proposal_id or raw_value
            outcome = _handle_owner_action(
                chat, board, db, event_sink, target,
                action_id, item_id, flagged=is_flag,
            )
            outcomes.append(outcome)
            logger.info("slack interaction outcome: kind=%s", outcome.get("kind"))
            continue
        logger.info(
            "slack interaction received: action=%s proposal=%s",
            action_id or verb or "unknown",
            proposal_id or "none",
        )
        if not is_approve and not is_cancel:
            outcomes.append({"action": action_id, "kind": "ignored"})
            continue
        saved = None
        if proposal_id and db is not None:
            try:
                from pam_pm.core import store as store_mod

                saved = store_mod.get_plan_proposal(db, proposal_id)
            except Exception:  # noqa: BLE001
                saved = None
        if saved is None or saved.get("status") != "pending":
            if chat is not None and target:
                chat.send_dm(target, UNKNOWN_PROPOSAL_REPLY)
            outcomes.append({"action": action_id, "kind": "unknown_proposal"})
            logger.info("slack interaction outcome: kind=unknown_proposal")
            continue
        if is_cancel:
            try:
                from pam_pm.core import store as store_mod

                store_mod.set_plan_proposal_status(db, proposal_id, "discarded")
            except Exception:  # noqa: BLE001
                logger.info("plan proposal discard store failed")
            if chat is not None and target:
                chat.send_dm(
                    target,
                    "Discarded that proposal. Nothing was created on the board.",
                )
            outcomes.append({"action": action_id, "kind": "cancelled"})
            logger.info("slack interaction outcome: kind=cancelled")
            continue
        if board is None:
            if chat is not None and target:
                chat.send_dm(target, UNKNOWN_PROPOSAL_REPLY)
            outcomes.append({"action": action_id, "kind": "unknown_proposal"})
            continue
        from pam_pm.agents.planner import approve_proposal

        created = approve_proposal(
            saved.get("proposal", []),
            board,
            db=db,
            dry_run=False,
            evidence_quote="Approved from Slack sticky-note proposal.",
        )
        try:
            from pam_pm.core import store as store_mod

            store_mod.set_plan_proposal_status(db, proposal_id, "approved")
        except Exception:  # noqa: BLE001
            logger.info("plan proposal approve store failed")
        count = len(created)
        word = "item" if count == 1 else "items"
        if chat is not None and target:
            chat.send_dm(
                target, f"Approved. Created {count} {word} on the board."
            )
        if event_sink is not None:
            try:
                event_sink(
                    {
                        "operation": "plan_approve",
                        "item_id": "",
                        "evidence_quote": (
                            f"Slack Approve applied proposal {proposal_id}."
                        ),
                        "status": "applied",
                        "detail": f"created {count} {word} from proposal {proposal_id}",
                    }
                )
            except Exception:  # noqa: BLE001
                logger.info("plan approve event sink failed")
        outcomes.append(
            {"action": action_id, "kind": "approved", "created": count}
        )
        logger.info("slack interaction outcome: kind=approved created=%d", count)
    replied = any(
        o.get("kind") in ("approved", "cancelled", "unknown_proposal", "accepted", "flagged")
        for o in outcomes
    )
    return {"kind": "block_actions", "replied": replied, "outcomes": outcomes}


def _handle_slack_text_message(
    payload: dict[str, Any],
    inner: dict[str, Any],
    *,
    chat: Any,
    board: Any,
    db: Any,
    model_client: Any,
) -> dict[str, Any]:
    """Resume an open interview from a Slack text message, else ignore.

    Bot and system messages are ignored. A user message resumes the
    newest open interview for that person when a database and a model
    client are available; without them the message is ignored exactly
    as before, so file flows and existing callers are unchanged.
    """
    subtype = str(inner.get("subtype", "") or "").strip()
    if subtype in ("bot_message", "message_changed", "message_deleted"):
        logger.info("slack event ignored: subtype=%s", subtype or "none")
        return {"kind": "ignored", "replied": False}
    user = str(inner.get("user", "") or "").strip()
    if inner.get("bot_id") and not user:
        logger.info("slack event ignored: bot message")
        return {"kind": "ignored", "replied": False}
    text = str(inner.get("text", "") or "")
    channel = str(inner.get("channel", "") or "").strip()
    person = user or channel
    if not text.strip() or not person or chat is None:
        logger.info("slack event ignored: message without files")
        return {"kind": "ignored", "replied": False}
    if db is None or model_client is None:
        logger.info("slack event ignored: interview resume not configured")
        return {"kind": "ignored", "replied": False}
    marker = str(
        inner.get("ts", "")
        or inner.get("client_msg_id", "")
        or payload.get("event_id", "")
        or ""
    ).strip()
    from pam_pm.agents import interviewer as interviewer_mod

    try:
        resolved = interviewer_mod.resolve_display_name(chat, user) if user else None
        outcome = interviewer_mod.resume_interview(
            resolved or person,
            text,
            chat,
            board,
            model_client,
            db=db,
            message_id=marker,
        )
    except Exception:  # noqa: BLE001
        logger.info("slack resume failed, keeping local result")
        return {"kind": "ignored", "replied": False}
    raw_kind = str(outcome.get("kind", "") or "")
    if raw_kind == "finished":
        resume_kind = "finished"
    elif raw_kind == "reply":
        resume_kind = "follow-up sent"
    elif raw_kind == "no_open_state":
        resume_kind = "no_open_state"
    elif raw_kind == "blocker_no_state":
        resume_kind = "blocker_no_state"
    elif "error" in raw_kind:
        resume_kind = "model_error"
    else:
        resume_kind = raw_kind or "unknown"
    logger.info("slack resume outcome: kind=%s", resume_kind)
    kinds = {
        "reply": "interview_reply",
        "finished": "interview_finished",
        "no_open_state": "interview_no_state",
        "blocker_no_state": "interview_blocker",
        "duplicate": "interview_duplicate",
        "write_failed": "interview_write_failed",
    }
    return {
        "kind": kinds.get(str(outcome.get("kind", "") or ""), "interview_resume"),
        "replied": bool(outcome.get("replied", False)),
        "person": person,
        "item_id": str(outcome.get("item_id", "") or ""),
        "resume": outcome,
    }


def _teams_graph_fields(event: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Graph message, bus event, or teams envelope to fields."""
    text = ""
    channel = ""
    user_id = ""
    message_id = ""
    attachments: list[Any] = []
    mimetype = ""
    filename = ""
    body = event.get("body")
    if isinstance(body, dict):
        from pam_pm.adapters.chat.teams_poll import message_text as _graph_text

        text = _graph_text(event)
        sender = event.get("from") or {}
        if isinstance(sender, dict):
            user = sender.get("user") or {}
            if isinstance(user, dict):
                user_id = str(user.get("id", "") or "")
        channel = str(event.get("chat_id", "") or user_id)
        message_id = str(event.get("id", "") or "")
        raw = event.get("attachments") or []
        attachments = list(raw) if isinstance(raw, list) else []
    else:
        text = str(event.get("text", "") or "")
        user_id = str(event.get("user_id", "") or "")
        channel = str(event.get("channel", "") or event.get("chat_id", "") or user_id)
        message_id = str(event.get("message_id", "") or event.get("id", "") or "")
        raw = event.get("attachments") or []
        attachments = list(raw) if isinstance(raw, list) else []
        mimetype = str(event.get("mimetype", "") or "")
        filename = str(event.get("filename", "") or "")
    return {
        "text": text,
        "channel": channel,
        "user_id": user_id,
        "message_id": message_id,
        "attachments": attachments,
        "mimetype": mimetype,
        "filename": filename,
    }


def _teams_attachment_file(attachments: list[Any]) -> tuple[str, str, str]:
    """Return file id, mimetype, and name for the first file attachment."""
    for item in attachments:
        if not isinstance(item, dict):
            continue
        content_type = str(item.get("contentType", "") or "").lower()
        if "reference" not in content_type and "file" not in content_type:
            continue
        content = item.get("content") or {}
        if isinstance(content, dict):
            url = str(content.get("contentUrl", "") or content.get("url", "") or "")
            name = str(content.get("name", "") or "")
        else:
            url = str(item.get("contentUrl", "") or "")
            name = str(item.get("name", "") or "")
        if url:
            return url, "", name
    return "", "", ""


def handle_teams_event(
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
    db: Any = None,
    model_client: Any = None,
    bot_id: str = "",
) -> dict[str, Any]:
    """Route a polled Teams message the same way Slack events route.

    Accepts a Graph chat message, a teams bus event, or a normalized
    teams_message dict. Pam's own sends and empty non file messages
    are ignored without a reply. File attachments go to the shared
    planner path with Approve buttons, exactly like Slack files, so
    proposals and approvals are unchanged. Text is logged under the
    Teams text capability and left for rules to answer from the bus,
    returning kind message with replied False.
    """
    payload = event if isinstance(event, dict) else {}
    if bot_id:
        sender = payload.get("from") or {}
        if isinstance(sender, dict):
            user = sender.get("user") or {}
            if isinstance(user, dict) and str(user.get("id", "") or "") == bot_id:
                logger.info("teams event ignored: own message")
                return {"kind": "ignored", "replied": False}
    fields = _teams_graph_fields(payload)
    channel = str(fields["channel"] or "")
    text = str(fields["text"] or "")
    file_id, file_mime, file_name = _teams_attachment_file(
        list(fields["attachments"])
    )
    mime = str(mimetype or "") or str(fields["mimetype"] or "") or file_mime
    name = str(filename or "") or str(fields["filename"] or "") or file_name
    if file_id and channel:
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
    if not text.strip():
        logger.info("teams event ignored: empty message")
        return {"kind": "ignored", "replied": False}
    if db is None or model_client is None:
        log_route(CAP_TEAMS_TEXT, text_model(), f"teams message in {channel or 'chat'}")
        return {
            "kind": "message",
            "capability": CAP_TEAMS_TEXT,
            "model": text_model(),
            "channel": channel,
            "user_id": str(fields["user_id"] or ""),
            "message_id": str(fields["message_id"] or ""),
            "text": text,
            "replied": False,
        }
    user_id = str(fields["user_id"] or "")
    person = user_id or channel
    if not person or chat is None:
        logger.info("teams event ignored: message without person")
        return {"kind": "ignored", "replied": False}
    from pam_pm.agents import interviewer as interviewer_mod

    try:
        resolved = (
            interviewer_mod.resolve_display_name(chat, user_id) if user_id else None
        )
        who = str(resolved or channel or person or "").strip()
        outcome = interviewer_mod.resume_interview(
            who or person,
            text,
            chat,
            board,
            model_client,
            db=db,
            message_id=str(fields["message_id"] or ""),
        )
    except Exception:  # noqa: BLE001
        logger.info("teams resume failed, keeping local result")
        return {"kind": "ignored", "replied": False}
    raw_kind = str(outcome.get("kind", "") or "")
    if raw_kind == "finished":
        resume_kind = "finished"
    elif raw_kind == "reply":
        resume_kind = "follow-up sent"
    elif raw_kind == "no_open_state":
        resume_kind = "no_open_state"
    elif raw_kind == "blocker_no_state":
        resume_kind = "blocker_no_state"
    elif "error" in raw_kind:
        resume_kind = "model_error"
    else:
        resume_kind = raw_kind or "unknown"
    logger.info("teams resume outcome: kind=%s", resume_kind)
    kinds = {
        "reply": "interview_reply",
        "finished": "interview_finished",
        "no_open_state": "interview_no_state",
        "blocker_no_state": "interview_blocker",
        "duplicate": "interview_duplicate",
        "write_failed": "interview_write_failed",
    }
    return {
        "kind": kinds.get(str(outcome.get("kind", "") or ""), "interview_resume"),
        "replied": bool(outcome.get("replied", False)),
        "person": person,
        "item_id": str(outcome.get("item_id", "") or ""),
        "resume": outcome,
    }
