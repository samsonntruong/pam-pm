"""Shared data schemas for interviews, blockers, retro actions, and writes."""

from pydantic import BaseModel, Field


class InterviewResult(BaseModel):
    """Structured per-item result produced by the interviewer (F3)."""

    status: str = Field(description="Current status of the item in plain words.")
    remaining: float | None = Field(
        default=None, description="Remaining work in hours, if known."
    )
    eta: str | None = Field(
        default=None, description="Expected completion date as ISO text, if known."
    )
    evidence_quote: str = Field(
        default="", description="Short verbatim quote backing the status."
    )
    blocker: str | None = Field(
        default=None, description="What blocks the item, if anything."
    )
    blocker_owner: str | None = Field(
        default=None, description="Who owns the blocking work, if anyone."
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Confidence from 0 to 1."
    )


class BlockerInfo(BaseModel):
    """Blocker intake result: what is blocked, by whom, and impact."""

    description: str = Field(description="What is blocked and how.")
    blocked_item_id: str | None = Field(
        default=None, description="ID of the waiting work item, if known."
    )
    blocking_item_id: str | None = Field(
        default=None, description="ID of the blocking work item, if known."
    )
    owner: str | None = Field(
        default=None, description="Owner of the blocking work, if known."
    )
    impact: str | None = Field(
        default=None, description="Impact of the blocker in plain words."
    )
    evidence_quote: str = Field(
        default="", description="Short verbatim quote backing the blocker."
    )


class RetroAction(BaseModel):
    """One retro action item, created on the board tagged retro-action."""

    title: str = Field(description="Short action title.")
    owner: str | None = Field(default=None, description="Action owner, if assigned.")
    description: str = Field(default="", description="Longer detail for the action.")
    work_item_id: str | None = Field(
        default=None, description="Board work item ID once created."
    )


class WriteRecord(BaseModel):
    """One board write with its evidence quote and lifecycle status."""

    id: int | None = Field(default=None, description="Store row ID once logged.")
    operation: str = Field(description="Write kind, e.g. update_state or comment.")
    item_id: str = Field(description="Target board item ID.")
    evidence_quote: str = Field(
        default="", description="Short verbatim quote behind this write."
    )
    status: str = Field(
        default="proposed", description="Lifecycle: proposed, applied, or undone."
    )
    detail: str = Field(default="", description="Extra detail as plain text or JSON.")
