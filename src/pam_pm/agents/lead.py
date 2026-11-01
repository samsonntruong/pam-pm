"""Lead synthesis agent: facts versus interpretation."""

from __future__ import annotations

from typing import Any


def _confidence_of(entry: Any) -> float:
    if isinstance(entry, dict):
        try:
            return float(entry.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    value = getattr(entry, "confidence", 0.0)
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _entry_id(key: Any, entry: Any, index: int) -> str:
    if isinstance(key, str) and key:
        return key
    if isinstance(entry, dict):
        for name in ("item_id", "id"):
            value = entry.get(name)
            if value:
                return str(value)
    for attr in ("item_id", "id"):
        value = getattr(entry, attr, None)
        if value:
            return str(value)
    return f"item-{index + 1}"


def _entry_text(entry: Any, name: str, default: str = "") -> str:
    if isinstance(entry, dict):
        return str(entry.get(name, default) or default)
    return str(getattr(entry, name, default) or default)


def synthesize_daily(items: Any, interviews: Any) -> dict[str, Any]:
    """Separate facts from interpretation and flag low confidence.

    High confidence entries (0.5 and above) become facts. Lower entries
    become interpretations and are listed in low_confidence flags.
    """
    if isinstance(interviews, dict):
        pairs = list(interviews.items())
    elif isinstance(interviews, (list, tuple)):
        pairs = [(None, entry) for entry in interviews]
    elif interviews is None:
        pairs = []
    else:
        pairs = [(None, interviews)]
    facts: list[dict[str, Any]] = []
    interpretations: list[dict[str, Any]] = []
    low_confidence: list[str] = []
    for index, (key, entry) in enumerate(pairs):
        item_id = _entry_id(key, entry, index)
        confidence = _confidence_of(entry)
        record = {
            "item_id": item_id,
            "status": _entry_text(entry, "status"),
            "evidence_quote": _entry_text(entry, "evidence_quote"),
            "confidence": confidence,
        }
        if confidence < 0.5:
            interpretations.append(record)
            low_confidence.append(item_id)
        else:
            facts.append(record)
    _ = items
    return {
        "facts": facts,
        "interpretations": interpretations,
        "low_confidence": low_confidence,
    }


class LeadAgent:
    """Synthesis helper that delegates to synthesize_daily."""

    def synthesize_daily(self, items: Any, interviews: Any) -> dict[str, Any]:
        """Separate facts from interpretation with confidence flags."""
        return synthesize_daily(items, interviews)
