"""Vision client for sticky-note reading with fallback."""

from __future__ import annotations

import base64
import importlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any

from pam_pm.core import router as router_mod

logger = logging.getLogger(__name__)

SAM_MODULE_CANDIDATES = ("sam3", "segment_anything_3", "sam_3")
PATH_SAM = "sam"
PATH_FALLBACK = "fallback"
SAM_API_PROMPT = "sticky note. Give one bounding box per sticky note."
SAM_API_TIMEOUT = 30


def _data_uri(raw: bytes) -> str:
    """Return a base64 data URI for raw image bytes.

    PNG and JPEG are detected from magic bytes; anything else falls
    back to PNG so the model still receives the real bytes.
    """
    if raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    else:
        mime = "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def sam_available() -> bool:
    """Return True when a SAM 3.1 module imports, else False.

    Never raises: any import problem means SAM is unavailable and the
    caller falls back to full image reading.
    """
    for name in SAM_MODULE_CANDIDATES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("SAM module %s unavailable: %s", name, exc)
            continue
        return True
    return False


def _sam_responses_url(base_url: str) -> str:
    """Return the Responses URL for a Meta base URL."""
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/responses"):
        return base
    if base.endswith("/v1"):
        return base + "/responses"
    return base + "/v1/responses"


def _numbers(text: str) -> list[float]:
    out: list[float] = []
    for tok in re.findall(r"-?\d+(?:\.\d+)?", str(text or "")):
        try:
            out.append(float(tok))
        except ValueError:
            continue
    return out


def _box_iou(a: list[float], b: list[float]) -> float:
    if list(a[:4]) == list(b[:4]):
        return 1.0
    try:
        ax0, ay0, ax1, ay1 = (float(v) for v in a[:4])
        bx0, by0, bx1, by1 = (float(v) for v in b[:4])
    except (TypeError, ValueError):
        return 0.0
    if max(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1) <= 1.5:
        return 1.0 if a[:4] == b[:4] else 0.0
    left, top = max(min(ax0, ax1), min(bx0, bx1)), max(min(ay0, ay1), min(by0, by1))
    right, bottom = min(max(ax0, ax1), max(bx0, bx1)), min(max(ay0, ay1), max(by0, by1))
    inter = max(0.0, right - left) * max(0.0, bottom - top)
    if inter <= 0.0:
        return 0.0
    area_a = max(0.0, max(ax0, ax1) - min(ax0, ax1)) * max(0.0, max(ay0, ay1) - min(ay0, ay1))
    area_b = max(0.0, max(bx0, bx1) - min(bx0, bx1)) * max(0.0, max(by0, by1) - min(by0, by1))
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def parse_sam_boxes(data: Any, raw: str = "") -> list[dict[str, Any]]:
    """Parse SAM boxes from a Responses payload, else masks as fallback.

    Looks for JSON bbox or box arrays first, then for <|box> tokens
    with nearby coordinates. When no coordinates parse but <|mask>
    tokens are present, returns one placeholder box per mask token.
    Never raises; returns an empty list when nothing usable is found.
    """
    try:
        hay = json.dumps(data) if not isinstance(data, str) else str(data)
    except (TypeError, ValueError):
        hay = str(data)
    hay = str(hay or "") + str(raw or "")
    if not hay:
        return []
    boxes: list[list[float]] = []
    try:
        for group in re.findall(
            r'"(?:bbox|box)"\s*:\s*\[([^\]]+)\]', hay, flags=re.IGNORECASE
        ):
            nums = _numbers(group)
            if len(nums) >= 4:
                boxes.append(nums[:4])
    except Exception:  # noqa: BLE001
        boxes = []
    if not boxes:
        try:
            for match in re.finditer(r"<\|box", hay, flags=re.IGNORECASE):
                window = hay[match.start() : match.start() + 400]
                nums = _numbers(window)
                if len(nums) >= 4:
                    boxes.append(nums[:4])
                if len(boxes) >= 50:
                    break
        except Exception:  # noqa: BLE001
            boxes = []
    if boxes:
        kept: list[list[float]] = []
        for cand in boxes[:50]:
            duplicate = False
            for have in kept:
                if _box_iou(cand, have) > 0.5:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(cand)
        return [
            {"bbox": b, "box": list(b), "id": i} for i, b in enumerate(kept)
        ]
    try:
        masks = hay.lower().count("<|mask")
    except Exception:  # noqa: BLE001
        masks = 0
    if masks > 0:
        count = min(int(masks), 50)
        return [{"bbox": [], "mask": True, "id": i} for i in range(count)]
    return []


def sam_api_boxes(
    image_bytes: bytes | bytearray | None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: int = SAM_API_TIMEOUT,
) -> list[dict[str, Any]]:
    """Segment one photo through SAM 3.1 on the Meta API.

    Sends the photo to META_VISION_MODEL asking for one bounding box
    per sticky note. Returns one box dict per note, or an empty list
    when config is missing, the call fails, or nothing parses. Never
    raises and never logs secrets or image bytes.
    """
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        return []
    base = (
        str(base_url).strip()
        if base_url is not None
        else os.environ.get("META_BASE_URL", "").strip()
    )
    key = (
        str(api_key).strip()
        if api_key is not None
        else os.environ.get("META_API_KEY", "").strip()
    )
    want_model = str(model or "").strip() or router_mod.vision_segment_model()
    if not base or not key or not want_model:
        return []
    url = _sam_responses_url(base)
    if not url:
        return []
    try:
        data_uri = _data_uri(bytes(image_bytes))
    except Exception:  # noqa: BLE001
        return []
    payload = {
        "model": want_model,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": SAM_API_PROMPT},
                    {"type": "input_image", "image_url": data_uri},
                ],
            }
        ],
    }
    try:
        body = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError):
        return []
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        },
    )
    try:
        wait = int(timeout) if int(timeout) > 0 else SAM_API_TIMEOUT
    except (TypeError, ValueError):
        wait = SAM_API_TIMEOUT
    try:
        with urllib.request.urlopen(req, timeout=wait) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        logger.debug("SAM API segment failed: status=%s", exc.code)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.debug("SAM API segment failed: %s", type(exc).__name__)
        return []
    if status != 200:
        logger.debug("SAM API segment failed: status=%s", status)
        return []
    try:
        parsed: Any = json.loads(text) if text else {}
    except json.JSONDecodeError:
        parsed = {}
    boxes = parse_sam_boxes(parsed, text)
    return boxes


def api_segmenter(
    image: bytes | bytearray | Any,
) -> tuple[list[bytes], list[dict[str, Any]]]:
    """Segment a photo via the Meta SAM API into crops plus boxes.

    Returns (crops, boxes) with one full photo crop per box so each
    crop can be read by Muse Spark vision. Returns ([], []) when the
    image is missing, config is missing, or parsing finds no notes.
    Never raises.
    """
    if not isinstance(image, (bytes, bytearray)) or not image:
        return ([], [])
    try:
        raw = bytes(image)
    except Exception:  # noqa: BLE001
        return ([], [])
    try:
        boxes = sam_api_boxes(raw)
    except Exception:  # noqa: BLE001
        return ([], [])
    if not boxes:
        return ([], [])
    coords = []
    for entry in boxes:
        bbox = (entry or {}).get("bbox", []) if isinstance(entry, dict) else []
        nums = [float(v) for v in (bbox or [])[:4]]
        if len(nums) == 4:
            coords.append(nums)
    if coords and all(
        abs(max(c[0], c[2]) - min(c[0], c[2])) < 8
        or abs(max(c[1], c[3]) - min(c[1], c[3])) < 8
        for c in coords
    ):
        return ([bytes(raw)], [{"bbox": [], "box": [], "id": 0}])
    crops: list[bytes] = []
    try:
        import io as _io

        from PIL import Image as _PILImage

        photo = _PILImage.open(_io.BytesIO(raw)).convert("RGB")
        width, height = photo.size
        for entry in boxes:
            bbox = (entry or {}).get("bbox", []) if isinstance(entry, dict) else []
            nums = [float(v) for v in (bbox or [])[:4]]
            if len(nums) < 4:
                crops.append(bytes(raw))
                continue
            x0, y0, x1, y1 = nums
            if max(nums) <= 1.5:
                x0, y0 = x0 * width, y0 * height
                x1, y1 = x1 * width, y1 * height
            left, top = max(0, int(min(x0, x1))), max(0, int(min(y0, y1)))
            right, bottom = min(width, int(max(x0, x1))), min(height, int(max(y0, y1)))
            if right - left < 8 or bottom - top < 8:
                crops.append(bytes(raw))
                continue
            buf = _io.BytesIO()
            photo.crop((left, top, right, bottom)).save(buf, format="PNG")
            crops.append(buf.getvalue())
    except Exception:  # noqa: BLE001
        crops = [bytes(raw) for _ in boxes]
    if len(crops) != len(boxes):
        crops = [bytes(raw) for _ in boxes]
    return (crops, list(boxes))


class VisionClient:
    """Read sticky notes from crops, falling back to the full image.

    SAM 3.1 segmentation is preferred. When no crops are available the
    caller uses read_full on the whole image as the fallback path via
    Muse Spark vision. Without a model client, reads return empty text
    and perform no network calls.
    """

    def __init__(
        self,
        model_client: Any = None,
        segmenter: Any = None,
        provider: str = "meta",
    ) -> None:
        self.model_client = model_client
        self.segmenter = segmenter
        self.provider = str(provider or "meta").strip().lower() or "meta"

    def segment(self, image: Any) -> list[Any]:
        """Split an image into note crops with SAM 3.1 when possible.

        Returns an empty list when SAM is missing or segmentation fails.
        SAM 3.1 is Meta only, so a non Meta provider always returns an
        empty list and the caller reads the whole image instead.
        Never raises for a missing or broken SAM install.
        """
        if image is None:
            return []
        if self.provider != "meta":
            logger.info("vision path used: fallback (custom provider, SAM is Meta only)")
            return []
        segmenter = self.segmenter
        if segmenter is None:
            if not sam_available():
                logger.info("vision path used: fallback (SAM 3.1 not installed)")
                return []
            logger.info("vision path used: fallback (no SAM segmenter wired)")
            return []
        try:
            crops = segmenter(image)
        except Exception as exc:  # noqa: BLE001
            logger.info("vision path used: fallback (SAM segment failed: %s)", exc)
            return []
        if not crops:
            return []
        return list(crops)

    def read_crops(self, crops: Sequence[Any] | None) -> list[str]:
        """Read one text per crop, empty when a crop has no text."""
        if not crops:
            return []
        items = list(crops)
        if self.model_client is None:
            return ["" for _ in items]
        readings: list[str] = []
        for crop in items:
            try:
                text = self._read_image(crop)
            except Exception as exc:  # noqa: BLE001
                logger.info("crop read failed, keeping empty text: %s", exc)
                text = ""
            readings.append(text)
        return readings

    def read_full(self, image: Any) -> str:
        """Read the whole image as fallback when crops are missing."""
        if image is None:
            return ""
        if self.model_client is None:
            logger.warning(
                "vision read_full without model_client: "
                "wire VisionClient(model_client=...) from the live Meta ModelClient"
            )
            return ""
        try:
            return self._read_image(image)
        except Exception as exc:  # noqa: BLE001
            logger.info("full image read failed: %s", exc)
            return ""

    def _read_image(self, image: Any) -> str:
        client = self.model_client
        if isinstance(image, (bytes, bytearray)):
            prompt = "Read the sticky notes in this photo, one per line."
            content: Any = [
                {"type": "text", "text": prompt},
                {"type": "input_image", "image_url": _data_uri(bytes(image))},
            ]
        else:
            content = f"Read the sticky notes in this image: {image}."
        messages = [{"role": "user", "content": content}]
        try:
            result = client.chat(messages)
        except TypeError:
            result = client.complete(str(content))
        if hasattr(result, "text"):
            return str(getattr(result, "text", "") or "")
        if isinstance(result, dict):
            return str(result.get("text", "") or "")
        return str(result or "")


def mask_label(mask: Any, index: int) -> str:
    """Return a short label for one SAM mask beside its reading.

    Real SAM output can be an int index, a string id, or a dict with
    segmentation details; each form renders as Mask N with a short
    detail suffix when one is available. A missing mask renders as a
    placeholder label so fallback photos still pair one mask slot
    beside every reading.
    """
    tag = f"Mask {index + 1}"
    if mask is None:
        return f"Placeholder {tag.lower()}"
    if isinstance(mask, bool):
        return tag
    if isinstance(mask, int):
        return tag if mask == index else f"{tag} (id {mask})"
    if isinstance(mask, str):
        text = mask.strip()
        if not text or text == str(index):
            return tag
        return f"{tag} ({text[:40]})"
    if isinstance(mask, dict):
        for key in ("label", "id", "name"):
            value = mask.get(key)
            if value not in (None, ""):
                return f"{tag} ({str(value)[:40]})"
        detail = mask.get("bbox", mask.get("box", mask.get("polygon")))
        if detail is not None:
            return f"{tag} (segmented)"
        return tag
    if isinstance(mask, (bytes, bytearray)):
        return f"{tag} ({len(mask)} bytes)"
    if isinstance(mask, (list, tuple)):
        return f"{tag} ({len(mask)} points)"
    text = str(mask).strip()
    if not text:
        return tag
    return f"{tag} ({text[:40]})"


def pair_masks_with_readings(photo: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Pair each SAM mask with its reading for the plan screen.

    Returns one entry per note with index, mask, mask_label, and
    reading, so the plan screen can render each mask beside its
    reading. SAM photos pair real masks with readings in order.
    Fallback photos carry no masks, so each reading pairs with a
    placeholder mask slot instead. Never raises on odd input.
    """
    if not isinstance(photo, dict):
        return []
    raw_readings = photo.get("readings", [])
    raw_masks = photo.get("masks", [])
    readings = list(raw_readings) if isinstance(raw_readings, (list, tuple)) else []
    masks = list(raw_masks) if isinstance(raw_masks, (list, tuple)) else []
    count = max(len(readings), len(masks))
    pairs: list[dict[str, Any]] = []
    for index in range(count):
        mask = masks[index] if index < len(masks) else None
        reading = readings[index] if index < len(readings) else ""
        pairs.append(
            {
                "index": index,
                "mask": mask,
                "mask_label": mask_label(mask, index),
                "reading": str(reading or ""),
            }
        )
    return pairs


def plan_from_photo(
    image_bytes: bytes | bytearray | None,
    vision: VisionClient | None = None,
    segmenter: Any = None,
    provider: str = "meta",
) -> dict[str, Any]:
    """Read sticky notes from a photo, trying SAM first, then fallback.

    Uses SAM 3.1 crops when a segmenter yields crops, else reads the full
    image with vision. SAM 3.1 is Meta only: a non Meta provider skips
    segmentation and reads the whole image. Never crashes on a missing
    SAM install: any SAM problem logs and falls back. Logs which path
    was used. Returns readings, masks, path, and notes_text.
    """
    want = str(provider or "meta").strip().lower() or "meta"
    client = vision or VisionClient(provider=want)
    if getattr(client, "provider", "meta") != "meta":
        want = str(getattr(client, "provider", want))
    segment_model = router_mod.vision_segment_model()
    read_model = router_mod.text_model()
    if want != "meta":
        text = client.read_full(image_bytes) if image_bytes else ""
        readings = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
        logger.info("vision path used: fallback (custom provider, whole image)")
        router_mod.log_route(router_mod.CAP_VISION_READ, read_model, "planner path=fallback")
        return {
            "readings": readings,
            "masks": [],
            "boxes": [],
            "path": PATH_FALLBACK,
            "notes_text": "\n".join(readings),
            "model": read_model,
        }
    router_mod.log_route(router_mod.CAP_VISION_SEGMENT, segment_model, "planner segment attempt")
    if segmenter is not None:
        crops: Sequence[Any] | None = None
        try:
            crops = segmenter(image_bytes)
        except Exception as exc:  # noqa: BLE001
            logger.info("vision path used: fallback (SAM segment failed: %s)", exc)
            router_mod.log_route(
                router_mod.CAP_VISION_READ, read_model, "planner path=fallback sam_failed"
            )
            crops = None
        if crops:
            readings = client.read_crops(list(crops))
            logger.info("vision path used: sam (%d crops)", len(readings))
            router_mod.log_route(
                router_mod.CAP_VISION_READ, read_model, f"planner path=sam crops={len(readings)}"
            )
            notes = [text for text in readings if text.strip()]
            return {
                "readings": readings,
                "masks": list(range(len(readings))),
                "boxes": [],
                "path": PATH_SAM,
                "notes_text": "\n".join(notes),
                "model": read_model,
                "segment_model": segment_model,
            }
        if crops is not None:
            logger.info("vision path used: fallback (SAM returned no crops)")
    else:
        try:
            api_crops, api_boxes = api_segmenter(image_bytes)
        except Exception:  # noqa: BLE001
            api_crops, api_boxes = [], []
        if api_crops and api_boxes:
            readings = client.read_crops(list(api_crops))
            logger.info("vision path used: sam (%d boxes)", len(api_boxes))
            router_mod.log_route(
                router_mod.CAP_VISION_READ,
                read_model,
                f"planner path=sam boxes={len(api_boxes)}",
            )
            notes = [text for text in readings if text.strip()]
            return {
                "readings": readings,
                "masks": list(api_boxes),
                "boxes": list(api_boxes),
                "path": PATH_SAM,
                "notes_text": "\n".join(notes),
                "model": read_model,
                "segment_model": segment_model,
            }
        found = client.segment(image_bytes)
        if found:
            readings = client.read_crops(found)
            logger.info("vision path used: sam (%d crops)", len(readings))
            router_mod.log_route(
                router_mod.CAP_VISION_READ, read_model, f"planner path=sam crops={len(readings)}"
            )
            notes = [text for text in readings if text.strip()]
            return {
                "readings": readings,
                "masks": list(range(len(readings))),
                "boxes": [],
                "path": PATH_SAM,
                "notes_text": "\n".join(notes),
                "model": read_model,
                "segment_model": segment_model,
            }
    text = client.read_full(image_bytes) if image_bytes else ""
    readings = [line.strip() for line in str(text).splitlines() if line.strip()]
    logger.info("vision path used: fallback (full image read)")
    router_mod.log_route(router_mod.CAP_VISION_READ, read_model, "planner path=fallback")
    return {
        "readings": readings,
        "masks": [],
        "boxes": [],
        "path": PATH_FALLBACK,
        "notes_text": "\n".join(readings),
        "model": read_model,
    }
