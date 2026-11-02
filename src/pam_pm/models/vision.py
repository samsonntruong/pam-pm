"""Vision client for sticky-note reading with fallback."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Sequence
from typing import Any

from pam_pm.core import router as router_mod

logger = logging.getLogger(__name__)

SAM_MODULE_CANDIDATES = ("sam3", "segment_anything_3", "sam_3")
PATH_SAM = "sam"
PATH_FALLBACK = "fallback"


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
                {"type": "input_image", "image_url": f"bytes:{len(image)}"},
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
