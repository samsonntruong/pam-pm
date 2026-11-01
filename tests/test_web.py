"""M3a web console skeleton tests."""

from fastapi.testclient import TestClient

from pam_pm.web.app import app

client = TestClient(app)
PAGES = ["/setup", "/rules", "/activity", "/board"]


def test_health_ok() -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_pages_load() -> None:
    for page in PAGES:
        resp = client.get(page)
        assert resp.status_code == 200, page
        assert "text/html" in resp.headers["content-type"], page


def test_setup_has_seven_steps_and_tests() -> None:
    text = client.get("/setup").text
    for n in range(1, 8):
        assert f"Step {n}" in text
    assert "/api/test/meta" in text
    assert "/api/test/slack" in text
    assert "/api/test/ado" in text


def test_test_endpoints_return_ok_or_plain_error() -> None:
    for which in ("meta", "slack", "ado"):
        resp = client.post(f"/api/test/{which}")
        assert resp.status_code == 200, which
        payload = resp.json()
        assert "ok" in payload
        assert "message" in payload
        assert isinstance(payload["message"], str)


def test_rules_has_template_toggles() -> None:
    text = client.get("/rules").text
    assert "morning" in text
    assert "follow_up" in text
    assert "blocker" in text


def test_activity_has_evidence_and_undo() -> None:
    text = client.get("/activity").text
    assert "evidence" in text.lower()
    assert "Undo" in text


def test_board_has_dependency_links() -> None:
    text = client.get("/board").text
    assert "Read only" in text
    assert "ependency" in text


def test_sse_endpoint_exists() -> None:
    resp = client.get("/api/activity/stream")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "data:" in resp.text


def test_no_em_dashes_in_rendered_html() -> None:
    for page in PAGES:
        text = client.get(page).text
        assert "\u2014" not in text, page
    css = client.get("/static/style.css").text
    assert "\u2014" not in css
