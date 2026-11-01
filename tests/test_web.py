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
    assert "Step 8" not in text
    headings = [
        "Step 1: Model provider",
        "Step 2: Slack",
        "Step 3: Azure DevOps",
        "Step 4: People",
        "Step 5: Rules",
        "Step 6: Safety",
        "Step 7: Done",
    ]
    positions = []
    for heading in headings:
        assert heading in text, heading
        positions.append(text.index(heading))
    assert positions == sorted(positions), "wizard order changed"
    assert "/api/test/provider" in text
    assert "/api/test/slack" in text
    assert "/api/test/ado" in text
    assert "/api/test/people" in text
    assert "/api/test/rules" in text
    assert "/api/test/safety" in text
    assert "Tell your team" in text
    assert "ONE internal Slack app once" in text


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


NAV_GROUPS = {
    "Plan": ["/board", "/activity", "/raid", "/plan", "/review"],
    "Run": ["/kickoff", "/triage", "/next-sprint", "/mid-sprint", "/add-task"],
    "Listen": ["/listen", "/meetings"],
    "Manage": ["/setup", "/rules", "/settings"],
}

ACTIVE_BY_URL = {
    "/board": "board",
    "/activity": "activity",
    "/raid": "raid",
    "/plan": "plan",
    "/review": "review",
    "/kickoff": "kickoff",
    "/triage": "triage",
    "/next-sprint": "next-sprint",
    "/mid-sprint": "mid-sprint",
    "/add-task": "add-task",
    "/listen": "listen",
    "/meetings": "meetings",
    "/setup": "setup",
    "/rules": "rules",
    "/settings": "settings",
}


def test_sidenav_has_four_labeled_groups() -> None:
    text = client.get("/setup").text
    for heading, links in NAV_GROUPS.items():
        assert f'<summary class="nav-heading">{heading}</summary>' in text, heading
        for link in links:
            assert f'href="{link}"' in text, link
    assert text.count('class="nav-group"') == 4
    positions = [text.index(f">{h}</summary>") for h in NAV_GROUPS]
    assert positions == sorted(positions), "group order changed"


def test_sidenav_active_link_on_every_route() -> None:
    for url in ACTIVE_BY_URL:
        text = client.get(url).text
        assert f'href="{url}" class="active"' in text, url
        assert text.count('class="active"') == 1, url


def test_board_cards_show_owner_avatar() -> None:
    from pam_pm.adapters.pm.base import WorkItem
    from pam_pm.web import app as app_mod

    class _AvatarBoard:
        supports_dependency_links = False
        supports_iterations = False
        supports_comments = False
        supports_close_iteration = False

        def list_items(self, limit: int = 500):  # type: ignore[no-untyped-def]
            return [
                WorkItem(
                    id="7",
                    title="Login form",
                    state="New",
                    fields={"owner": "Ana"},
                )
            ][:limit]

        def list_states(self) -> list[str]:
            return ["New", "Active", "Blocked", "Done"]

        def list_links(self, item_id: str) -> list:  # type: ignore[type-arg]
            return []

    app_mod.set_board_adapter("default", _AvatarBoard())
    try:
        text = client.get("/board").text
        assert "Login form" in text
        assert 'class="avatar"' in text
        assert ">AN<" in text
    finally:
        app_mod.reset_board_state()


def test_no_em_dashes_in_rendered_html() -> None:
    for page in PAGES:
        text = client.get(page).text
        assert "\u2014" not in text, page
    css = client.get("/static/style.css").text
    assert "\u2014" not in css
