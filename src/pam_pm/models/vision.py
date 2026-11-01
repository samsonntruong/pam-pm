"""Vision client for sticky-note reading with fallback."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class VisionClient:
    """Read sticky notes from crops, falling back to the full image.

    SAM 3.1 segmentation is preferred. When no crops are available the
    caller uses read_full on the whole image as the fallback path.
    """

    def read_crops(self, crops: Sequence[Any] | None) -> list[str]:
        """Read one text per crop, empty when a crop has no text."""
        if not crops:
            return []
        return ["" for _ in list(crops)]

    def read_full(self, image: Any) -> str:
        """Read the whole image as fallback when crops are missing."""
        _ = image
        return ""
