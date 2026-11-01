"""WP2a native board web layer tests with fake adapters."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pam_pm.adapters.pm.base import Comment, Iteration, Link, PMAdapter, WorkItem


def _setup_token() -> str:
    try:
        import pam_pm.web.app as _m

        return _m.get_setup_token()
    except Exception:  # noqa: BLE001
        import os

        return os.environ.get("PAM_SETUP_TOKEN", "")



class FakeNative(PMAdapter):
    """In-memory native board with closeable sprints."""

    supports_dependency_links = True
    supports_iterations = True
    supports_comments = True
    supports_close_iteration = True

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._links: list[Link] = []
        self._comments: dict[str, list[Comment]] = {}
        self._iterations: dict[str, Iteration] = {}
        self._closed: set[str] = set()
        self._next_id = 1
        self._next_iter = 1
        self._next_comment = 1
        self._clock = 0

    def _stamp(self) -> str:
        self._clock += 1
        return f"2026-02-01T00:00:{self._clock:02d}Z"

    def _item(self, item_id: str) -> WorkItem:
        if item_id not in self._items:
            raise ValueError(f"unknown item: {item_id}")
        entry = self._items[item_id]
        return WorkItem(
            id=item_id,
            title=str(entry["title"]),
            state=str(entry["state"]),
            changed=str(entry["changed"]),
            fields=dict(entry["fields"]),
        )

    def list_items(self, limit: int = 50) -> list[WorkItem]:
        ids = sorted(self._items, key=int)[:limit]
        return [self._item(i) for i in ids]

    def get_item(self, item_id: str) -> WorkItem:
        return self._item(item_id)

    def create_item(
        self, title: str, fields: dict[str, Any] | None = None
    ) -> WorkItem:
        item_id = str(self._next_id)
        self._next_id += 1
        given = dict(fields or {})
        state = str(given.pop("state", "New"))
        self._items[item_id] = {
            "title": title,
            "state": state,
            "changed": self._stamp(),
            "fields": given,
        }
        return self._item(item_id)

    def update_item(
        self, item_id: str, fields: dict[str, Any]
    ) -> WorkItem:
        current = self._item(item_id)
        given = dict(fields)
        title = str(given.pop("title", current.title))
        state = str(given.pop("state", current.state))
        rest = dict(current.fields)
        rest.update(given)
        self._items[item_id] = {
            "title": title,
            "state": state,
            "changed": self._stamp(),
            "fields": rest,
        }
        return self._item(item_id)

    def delete_item(self, item_id: str) -> None:
        if item_id not in self._items:
            raise ValueError(f"unknown item: {item_id}")
        del self._items[item_id]

    def list_states(self) -> list[str]:
        return ["New", "Active", "Blocked", "Done"]

    def close_item(self, item_id: str) -> WorkItem:
        return self.update_item(item_id, {"state": "Done"})

    def list_iterations(self) -> list[Iteration]:
        return [self._iterations[k] for k in sorted(self._iterations, key=int)]

    def create_iteration(
        self, name: str, start_date: str = "", end_date: str = ""
    ) -> Iteration:
        label = name.strip()
        if not label:
            raise ValueError("iteration name is required")
        ident = str(self._next_iter)
        self._next_iter += 1
        entry = Iteration(
            id=ident,
            name=label,
            start_date=start_date,
            end_date=end_date,
            path=label,
        )
        self._iterations[ident] = entry
        return entry

    def close_iteration(self, iteration_id: str) -> Iteration:
        if iteration_id not in self._iterations:
            raise ValueError(f"unknown iteration: {iteration_id}")
        self._closed.add(iteration_id)
        return self._iterations[iteration_id]

    def closed_iteration_ids(self) -> set[str]:
        return set(self._closed)

    def set_item_iteration(
        self, item_id: str, iteration_id: str
    ) -> WorkItem:
        if iteration_id and iteration_id not in self._iterations:
            raise ValueError(f"unknown iteration: {iteration_id}")
        return self.update_item(item_id, {"iteration_id": iteration_id})

    def add_link(self, source_id: str, target_id: str, rel: str) -> Link:
        if "Dependency" not in rel:
            raise ValueError(f"unknown link rel: {rel}")
        self._item(source_id)
        self._item(target_id)
        link = Link(source_id=source_id, target_id=target_id, rel=rel)
        self._links.append(link)
        return link

    def list_links(self, item_id: str) -> list[Link]:
        return [
            link
            for link in self._links
            if link.source_id == item_id or link.target_id == item_id
        ]

    def list_comments(self, item_id: str) -> list[Comment]:
        self._item(item_id)
        return list(self._comments.get(item_id, []))

    def add_comment(
        self, item_id: str, text: str, author: str = ""
    ) -> Comment:
        self._item(item_id)
        body = text.strip()
        if not body:
            raise ValueError("comment text is required")
        comment = Comment(
            id=str(self._next_comment),
            item_id=item_id,
            text=body,
            author=author.strip(),
            created=self._stamp(),
        )
        self._next_comment += 1
        self._comments.setdefault(item_id, []).append(comment)
        return comment

    def poll_changes(self, since: str | None = None) -> list[WorkItem]:
        items = [self._item(i) for i in sorted(self._items, key=int)]
        if since:
            items = [item for item in items if item.changed > since]
        return items


class FakeAdo(PMAdapter):
    """ADO shaped read surface with System.* fields, writes rejected."""

    supports_dependency_links = True
    supports_iterations = True
    supports_comments = True

    def __init__(self) -> None:
        self._items = {
            "101": WorkItem(
                id="101",
                title="Login form",
                state="To Do",
                changed="2026-01-01T00:00:00Z",
                fields={
                    "System.Title": "Login form",
                    "System.State": "To Do",
                    "System.AssignedTo": "Ana",
                },
            ),
            "102": WorkItem(
                id="102",
                title="API keys",
                state="In Progress",
                changed="2026-01-02T00:00:00Z",
                fields={
                    "System.Title": "API keys",
                    "System.State": "In Progress",
                    "System.AssignedTo": "Sam",
                },
            ),
        }

    def list_items(self, limit: int = 50) -> list[WorkItem]:
        return list(self._items.values())[:limit]

    def get_item(self, item_id: str) -> WorkItem:
        if item_id not in self._items:
            raise ValueError(f"unknown item: {item_id}")
        return self._items[item_id]

    def create_item(
        self, title: str, fields: dict[str, Any] | None = None
    ) -> WorkItem:
        raise ValueError("read only in Pam")

    def update_item(
        self, item_id: str, fields: dict[str, Any]
    ) -> WorkItem:
        raise ValueError("read only in Pam")

    def delete_item(self, item_id: str) -> None:
        raise ValueError("read only in Pam")

    def list_states(self) -> list[str]:
        return ["New", "Active", "Resolved", "Closed"]

    def close_item(self, item_id: str) -> WorkItem:
        raise ValueError("read only in Pam")

    def list_iterations(self) -> list[Iteration]:
        return [
            Iteration(
                id="s1",
                name="Sprint 1",
                start_date="2026-01-05",
                end_date="2026-01-18",
                path="proj\\Sprint 1",
            )
        ]

    def create_iteration(
        self, name: str, start_date: str = "", end_date: str = ""
    ) -> Iteration:
        raise ValueError("read only in Pam")

    def set_item_iteration(
        self, item_id: str, iteration_id: str
    ) -> WorkItem:
        raise ValueError("read only in Pam")

    def add_link(self, source_id: str, target_id: str, rel: str) -> Link:
        raise ValueError("read only in Pam")

    def list_links(self, item_id: str) -> list[Link]:
        if item_id in ("101", "102"):
            return [
                Link(
                    source_id="101",
                    target_id="102",
                    rel="System.LinkTypes.Dependency-Forward",
                )
            ]
        return []

    def list_comments(self, item_id: str) -> list[Comment]:
        return []

    def add_comment(
        self, item_id: str, text: str, author: str = ""
    ) -> Comment:
        raise ValueError("read only in Pam")

    def poll_changes(self, since: str | None = None) -> list[WorkItem]:
        return []


@pytest.fixture()
def boards(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("ADO_ORG_URL", "https://dev.azure.com/fakeorg")
    monkeypatch.setenv("ADO_PROJECT", "fakeproj")
    monkeypatch.delenv("ADO_PAT", raising=False)
    monkeypatch.setenv("META_API_KEY", "secret-model-key")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "secret-slack-token")
    import pam_pm.web.app as app_mod

    app_mod.reset_board_state()
    native = FakeNative()
    ado = FakeAdo()
    app_mod.set_board_adapter("nativeproj", native)
    app_mod.set_board_adapter("adoproj", ado)
    client = TestClient(app_mod.app)
    client.post(
        "/api/setup/backend",
        json={"token": _setup_token(), "project": "nativeproj", "backend": "pam_board"},
    )
    client.post(
        "/api/setup/backend",
        json={"token": _setup_token(), "project": "adoproj", "backend": "azure_devops"},
    )
    yield SimpleNamespace(client=client, native=native, ado=ado, app=app_mod)
    app_mod.reset_board_state()


def _create(client: TestClient, title: str = "Ship it") -> dict[str, Any]:
    resp = client.post(
        "/api/board/items?project=nativeproj",
        json={"title": title, "owner": "Sam", "estimate": "8"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    return payload


def test_create_logs_write_activity_and_undo(boards) -> None:  # type: ignore[no-untyped-def]
    payload = _create(boards.client)
    assert payload["item"]["title"] == "Ship it"
    assert payload["item"]["state"] == "New"
    write_id = payload["write_id"]
    activity = boards.client.get("/activity").text
    assert "create" in activity
    assert "Ship it" in activity
    undo = boards.client.post(f"/api/undo/{write_id}")
    assert undo.status_code == 200
    assert undo.json()["ok"] is True


def test_human_edits_logged_as_human(boards) -> None:  # type: ignore[no-untyped-def]
    from pam_pm.core import store as store_mod
    from pam_pm.core.config import Config

    _create(boards.client, "Human check")
    writes = store_mod.list_writes(Config().db_path())
    assert writes
    assert '"actor": "human"' in writes[-1].detail
    assert writes[-1].status == "applied"


def test_edit_and_move_item(boards) -> None:  # type: ignore[no-untyped-def]
    item_id = _create(boards.client)["item"]["id"]
    edited = boards.client.patch(
        f"/api/board/items/{item_id}?project=nativeproj",
        json={"owner": "Ana", "remaining": "3"},
    )
    assert edited.json()["item"]["owner"] == "Ana"
    assert edited.json()["item"]["remaining"] == "3"
    moved = boards.client.post(
        f"/api/board/items/{item_id}/move?project=nativeproj",
        json={"state": "Done"},
    )
    assert moved.json()["item"]["state"] == "Done"
    bad = boards.client.post(
        f"/api/board/items/{item_id}/move?project=nativeproj",
        json={"state": "Nope"},
    )
    assert bad.status_code == 400
    missing = boards.client.patch(
        "/api/board/items/999?project=nativeproj", json={"owner": "x"}
    )
    assert missing.status_code == 404


def test_move_to_menu_present_in_html(boards) -> None:  # type: ignore[no-untyped-def]
    _create(boards.client)
    text = boards.client.get("/board?project=nativeproj").text
    assert "Move to" in text
    for column in ("New", "Active", "Blocked", "Done"):
        assert column in text
    assert "Backlog" in text
    assert "Planning" in text
    assert "item-drawer" in text
    assert "board.js" in text


def test_link_items(boards) -> None:  # type: ignore[no-untyped-def]
    first = _create(boards.client, "First")["item"]["id"]
    second = _create(boards.client, "Second")["item"]["id"]
    resp = boards.client.post(
        "/api/board/links?project=nativeproj",
        json={"source_id": first, "target_id": second},
    )
    assert resp.status_code == 200
    link = resp.json()["link"]
    assert link["label"] == f"{first} blocks {second}"
    detail = boards.client.get(
        f"/api/board/items/{first}?project=nativeproj"
    ).json()
    assert len(detail["links"]) == 1
    bad = boards.client.post(
        "/api/board/links?project=nativeproj",
        json={"source_id": first, "target_id": second, "rel": "Nope"},
    )
    assert bad.status_code == 400


def test_sprint_set_and_close(boards) -> None:  # type: ignore[no-untyped-def]
    made = boards.client.post(
        "/api/board/sprints?project=nativeproj",
        json={"name": "Sprint 1", "start_date": "2026-01-05"},
    ).json()
    assert made["ok"] is True
    sprint_id = made["sprint"]["id"]
    item_id = _create(boards.client)["item"]["id"]
    moved = boards.client.post(
        f"/api/board/items/{item_id}/sprint?project=nativeproj",
        json={"iteration_id": sprint_id},
    )
    assert moved.json()["item"]["iteration_id"] == sprint_id
    page = boards.client.get("/board?project=nativeproj").text
    assert "Sprint 1" in page
    assert "Close sprint" in page
    closed = boards.client.post(
        f"/api/board/sprints/{sprint_id}/close?project=nativeproj", json={}
    )
    assert closed.json()["ok"] is True
    data = boards.client.get("/api/board?project=nativeproj").json()
    assert data["iterations"][0]["status"] == "closed"
    missing = boards.client.post(
        "/api/board/sprints/999/close?project=nativeproj", json={}
    )
    assert missing.status_code == 404


def test_csv_round_trip(boards) -> None:  # type: ignore[no-untyped-def]
    _create(boards.client, "Alpha")
    _create(boards.client, "Beta")
    export = boards.client.get("/api/board/export?project=nativeproj")
    assert export.status_code == 200
    assert "text/csv" in export.headers["content-type"]
    assert "Alpha" in export.text
    assert "Beta" in export.text
    boards.app.set_board_adapter("native2", FakeNative())
    boards.client.post(
        "/api/setup/backend",
        json={"token": _setup_token(), "project": "native2", "backend": "pam_board"},
    )
    imported = boards.client.post(
        "/api/board/import?project=native2", json={"csv": export.text}
    ).json()
    assert imported["ok"] is True
    assert imported["created"] == 2
    assert imported["skipped"] == 0
    data = boards.client.get("/api/board?project=native2").json()
    titles = {item["title"] for item in data["items"]}
    assert {"Alpha", "Beta"} <= titles


def test_ado_read_view_with_linkouts(boards) -> None:  # type: ignore[no-untyped-def]
    text = boards.client.get("/board?project=adoproj").text
    assert "Read only" in text
    assert "Open in Azure DevOps" in text
    assert "Dependency links" in text
    assert "Login form" in text
    assert (
        "https://dev.azure.com/fakeorg/fakeproj/_workitems/edit/101" in text
    )
    assert "Move to" not in text
    denied = boards.client.post(
        "/api/board/items?project=adoproj", json={"title": "Nope"}
    )
    assert denied.status_code == 403
    move = boards.client.post(
        "/api/board/items/101/move?project=adoproj", json={"state": "Done"}
    )
    assert move.status_code == 403
    close = boards.client.post(
        "/api/board/sprints/s1/close?project=adoproj", json={}
    )
    assert close.status_code == 403


def test_capabilities_and_list(boards) -> None:  # type: ignore[no-untyped-def]
    data = boards.client.get("/api/board?project=nativeproj").json()
    assert data["ok"] is True
    assert data["editable"] is True
    assert data["capabilities"]["close_iteration"] is True
    assert data["capabilities"]["dependency_links"] is True
    assert data["states"] == ["New", "Active", "Blocked", "Done"]
    ado = boards.client.get("/api/board?project=adoproj").json()
    assert ado["editable"] is False
    assert ado["capabilities"]["close_iteration"] is False
    assert len(ado["items"]) == 2


def test_no_em_dashes_or_secrets(boards) -> None:  # type: ignore[no-untyped-def]
    _create(boards.client)
    for url in ("/board?project=nativeproj", "/board?project=adoproj"):
        text = boards.client.get(url).text
        assert "\u2014" not in text
        assert "secret-model-key" not in text
        assert "secret-slack-token" not in text
    css = boards.client.get("/static/style.css").text
    assert "\u2014" not in css
    js = boards.client.get("/static/board.js")
    assert js.status_code == 200
    assert "\u2014" not in js.text
