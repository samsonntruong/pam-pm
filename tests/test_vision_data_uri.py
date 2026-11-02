"""Vision fallback must send real image bytes, not a placeholder."""

from __future__ import annotations

import base64
from typing import Any

from pam_pm.models.vision import VisionClient

PNG = bytes.fromhex("89504e470d0a1a0a") + b"fakepngdata1234"
JPEG = bytes.fromhex("ffd8ffe0") + b"fakejpegdata5678"


class FakeClient:
    def __init__(self) -> None:
        self.seen: list[Any] = []

    def chat(self, messages: Any) -> Any:
        self.seen.append(messages)
        return {"text": "note one"}


def _image_part(messages: Any) -> dict[str, Any]:
    content = messages[0]["content"]
    assert isinstance(content, list), content
    imgs = [p for p in content if p.get("type") == "input_image"]
    assert len(imgs) == 1, content
    return imgs[0]


def test_read_full_png_sends_data_uri_with_real_bytes() -> None:
    fake = FakeClient()
    out = VisionClient(model_client=fake).read_full(PNG)
    assert out == "note one"
    url = _image_part(fake.seen[0])["image_url"]
    assert url.startswith("data:image/png;base64,"), url
    assert base64.b64decode(url.split(",", 1)[1]) == PNG


def test_read_full_jpeg_sends_data_uri_with_real_bytes() -> None:
    fake = FakeClient()
    VisionClient(model_client=fake).read_full(JPEG)
    url = _image_part(fake.seen[0])["image_url"]
    assert url.startswith("data:image/jpeg;base64,"), url
    assert base64.b64decode(url.split(",", 1)[1]) == JPEG


def test_read_full_unknown_bytes_falls_back_to_png() -> None:
    fake = FakeClient()
    raw = b"not-an-image-at-all"
    VisionClient(model_client=fake).read_full(raw)
    url = _image_part(fake.seen[0])["image_url"]
    assert url.startswith("data:image/png;base64,"), url
    assert base64.b64decode(url.split(",", 1)[1]) == raw


def test_read_crops_each_sends_data_uri() -> None:
    fake = FakeClient()
    out = VisionClient(model_client=fake).read_crops([PNG, JPEG])
    assert out == ["note one", "note one"]
    assert len(fake.seen) == 2
    for msgs, raw, mime in (
        (fake.seen[0], PNG, "image/png"),
        (fake.seen[1], JPEG, "image/jpeg"),
    ):
        url = _image_part(msgs)["image_url"]
        assert url.startswith(f"data:{mime};base64,"), url
        assert base64.b64decode(url.split(",", 1)[1]) == raw
