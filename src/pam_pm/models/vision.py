"""Vision client for sticky-note reading with fallback."""

from __future__ import annotations

import base64
import importlib
import logging
from collections.abc import Sequence
from typing import Any

from pam_pm.core import router as router_mod

logger = logging.getLogger(__name__)

SAM_MODULE_CANDIDATES = ("sam3", "segment_anything_3", "sam_3")
PATH_SAM = "sam"
PATH_FALLBACK = "fallback"


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
                "path": PATH_SAM,
                "notes_text": "\n".join(notes),
                "model": read_model,
                "segment_model": segment_model,
            }
        if crops is not None:
            logger.info("vision path used: fallback (SAM returned no crops)")
    else:
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
        "path": PATH_FALLBACK,
        "notes_text": "\n".join(readings),
        "model": read_model,
    }
