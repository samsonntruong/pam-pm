"""M3b plan from photo tests: mocked vision, no network."""

import logging

from pam_pm.adapters.pm.sqlite_board import SQLiteBoard
from pam_pm.agents.planner import approve_proposal, propose_sprint
from pam_pm.models.vision import VisionClient, plan_from_photo

PHOTO_FIXTURE = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB\x60\x82"
)


class FakeVision(VisionClient):
    def __init__(self, text="Login form\nAPI keys"):
        super().__init__()
        self.text = text
        self.full_reads = 0

    def read_full(self, image):
        self.full_reads += 1
        assert image == PHOTO_FIXTURE
        return self.text


def test_photo_fixture_produces_proposal_without_network():
    photo = plan_from_photo(PHOTO_FIXTURE, vision=FakeVision())
    assert photo["path"] == "fallback"
    assert photo["readings"] == ["Login form", "API keys"]
    proposal = propose_sprint(photo["readings"])
    assert len(proposal) == 2
    for draft in proposal:
        assert draft["title"]
        assert draft["type"] == "Task"
        assert "owner" in draft and "estimate" in draft
    assert [d["title"] for d in proposal] == ["Login form", "API keys"]


def test_propose_sprint_accepts_notes_text_and_note_dicts():
    proposal = propose_sprint("One\nTwo\n")
    assert [d["title"] for d in proposal] == ["One", "Two"]
    proposal = propose_sprint(
        [{"title": "Deploy", "type": "Task", "owner": "Ben", "estimate": 3}]
    )
    assert proposal[0]["owner"] == "Ben"
    assert proposal[0]["estimate"] == 3.0
    assert propose_sprint(None) == []
    assert propose_sprint("") == []


def test_approve_flow_creates_items_via_adapter():
    board = SQLiteBoard()
    try:
        proposal = propose_sprint("Login form\nAPI keys")
        created = approve_proposal(proposal, board)
        assert [item.title for item in created] == ["Login form", "API keys"]
        assert [item.id for item in board.list_items()] == ["1", "2"]
    finally:
        board.close()


def test_plan_from_photo_never_crashes_on_missing_sam(caplog):
    def broken_segmenter(image_bytes):
        raise RuntimeError("SAM 3.1 not installed")

    with caplog.at_level(logging.INFO, logger="pam_pm.models.vision"):
        photo = plan_from_photo(
            PHOTO_FIXTURE, vision=FakeVision(), segmenter=broken_segmenter
        )
    assert photo["path"] == "fallback"
    assert photo["readings"] == ["Login form", "API keys"]
    assert "vision path used: fallback" in caplog.text


class _FakeModelClient:
    def __init__(self, text="Alpha\nBeta"):
        self.text = text
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return {"text": self.text}


def test_plan_from_photo_with_wired_fake_client_returns_readings():
    vision = VisionClient(model_client=_FakeModelClient("Alpha\nBeta"))
    photo = plan_from_photo(PHOTO_FIXTURE, vision=vision)
    assert photo["readings"] == ["Alpha", "Beta"]
    assert photo["path"] == "fallback"


def test_sam_path_used_when_crops_exist():
    vision = FakeVision()

    def segmenter(image_bytes):
        assert image_bytes == PHOTO_FIXTURE
        return ["crop-a", "crop-b"]

    readings = ["Fix login", "Write docs"]
    vision.read_crops = lambda crops: readings
    photo = plan_from_photo(PHOTO_FIXTURE, vision=vision, segmenter=segmenter)
    assert photo["path"] == "sam"
    assert photo["masks"] == [0, 1]
    assert photo["readings"] == readings
