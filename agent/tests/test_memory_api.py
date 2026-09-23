"""The memory panel's API: notes, lessons, remembered runs, ratings, search, usage, reset."""

from __future__ import annotations

import pytest
from fakes import FakeEnv
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vilagent.control import Control
from vilagent.memory.store import Episode, MemoryStore
from vilagent.runs.manager import RunManager
from vilagent.runs.records import RunRecords
from vilagent.server import memory_api, state
from vilagent.server.deps import internal_auth_headers
from vilagent.server.runtime import Runtime

BASE = "/api/computer-use"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_FILE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(memory_api, "current_embedder", lambda: None)  # keyword search only
    app = FastAPI()
    app.include_router(memory_api.router)
    control = Control()
    app.state.runtime = Runtime(control=control, desktop=FakeEnv(control=control), browser=FakeEnv("browser", control=control), runs=RunManager(RunRecords(tmp_path / "runs")))
    with TestClient(app, headers=internal_auth_headers()) as test_client:
        # Open the store on the client's own event loop, like the gateway lifespan does.
        app.state.runtime.memory = test_client.portal.call(MemoryStore.open, tmp_path / "memory.sqlite")
        yield test_client
        test_client.portal.call(app.state.runtime.memory.close)


def _store(client) -> MemoryStore:
    return client.app.state.runtime.memory


def test_notes_are_filed_edited_switched_off_and_deleted(client):
    created = client.post(f"{BASE}/memory/lessons", json={"key_type": "domain", "key": "https://www.Mail.Google.com/u/0", "text": "  Compose is   top left. "})
    assert created.status_code == 201 and created.json()["key"] == "mail.google.com"
    note_id = created.json()["id"]

    assert client.post(f"{BASE}/memory/lessons", json={"key_type": "app", "key": "  ", "text": "x"}).status_code == 422
    (lesson,) = client.get(f"{BASE}/memory/lessons").json()
    assert (lesson["source"], lesson["text"], "embedding" in lesson) == ("operator", "Compose is top left.", False)

    assert client.patch(f"{BASE}/memory/lessons/{note_id}", json={"disabled": True, "text": "Compose: top left."}).status_code == 200
    (lesson,) = client.get(f"{BASE}/memory/lessons").json()
    assert (lesson["disabled"], lesson["text"]) == (1, "Compose: top left.")
    assert client.patch(f"{BASE}/memory/lessons/{note_id}", json={"key_type": "app", "key": "Outlook"}).json()["key"] == "outlook"
    assert client.patch(f"{BASE}/memory/lessons/{note_id}", json={}).status_code == 422

    assert client.delete(f"{BASE}/memory/lessons/{note_id}").status_code == 200
    assert client.delete(f"{BASE}/memory/lessons/{note_id}").status_code == 404
    assert client.patch(f"{BASE}/memory/lessons/{'0' * 32}", json={"disabled": True}).status_code == 404


def test_runs_are_rated_searched_and_their_usage_listed(client):
    store = _store(client)
    episode_id = client.portal.call(store.add_episode, Episode(run_id="run-7", task_text="Draw a circle in Paint", approach="plan_execute", plan_outline=[]))
    client.portal.call(store.record_usage, "run-9", [("episodes", episode_id)])

    assert client.post(f"{BASE}/runs/run-7/rating", json={"rating": "bad"}).status_code == 200
    assert client.get(f"{BASE}/memory/episodes").json()[0]["rating"] == "bad"
    assert client.post(f"{BASE}/runs/unknown-run/rating", json={"rating": "good"}).status_code == 404
    assert client.patch(f"{BASE}/memory/episodes/{episode_id}", json={"rating": None}).json()["rating"] is None

    found = client.get(f"{BASE}/memory/search", params={"q": "paint circle"}).json()
    assert [row["id"] for row in found["episodes"]] == [episode_id] and found["lessons"] == []
    used = client.get(f"{BASE}/runs/run-9/memory").json()
    assert [row["id"] for row in used["episodes"]] == [episode_id] and used["lessons"] == []

    assert client.delete(f"{BASE}/memory/episodes/{episode_id}").status_code == 200
    assert client.get(f"{BASE}/runs/run-9/memory").json()["episodes"] == []


def test_clear_needs_confirmation_and_the_toggle_persists(client):
    client.post(f"{BASE}/memory/lessons", json={"key_type": "general", "text": "Wait for pages to load."})

    assert client.post(f"{BASE}/memory/clear", json={}).status_code == 422
    assert client.post(f"{BASE}/memory/clear", json={"confirm": True}).status_code == 200
    assert client.get(f"{BASE}/memory/lessons").json() == []

    assert client.get(f"{BASE}/memory/enabled").json() == {"enabled": True}
    client.post(f"{BASE}/memory/enabled", json={"enabled": False})
    assert client.get(f"{BASE}/memory/enabled").json() == {"enabled": False}


def test_an_entry_or_a_whole_table_can_be_embedded_on_request(client, monkeypatch):
    """The re-embed buttons: keyword-only says so, and a model gives every row a vector."""
    note = client.post(f"{BASE}/memory/lessons", json={"key_type": "general", "key": "general", "text": "Close cookie banners first."}).json()["id"]
    client.post(f"{BASE}/memory/lessons", json={"key_type": "app", "key": "notepad", "text": "Type into the editor area."})
    assert [row["embedded"] for row in client.get(f"{BASE}/memory/lessons").json()] == [False, False]

    refused = client.post(f"{BASE}/memory/embed", json={"entry_type": "lessons"})
    assert refused.status_code == 409 and "keywords only" in refused.json()["detail"]

    class _Embedder:
        model_id = "test:v1"

        async def embed(self, texts):
            return [b"\x00\x00\x80?" for _ in texts]  # one float32 each, enough to be stored

    monkeypatch.setattr(memory_api, "current_embedder", lambda: _Embedder())

    one = client.post(f"{BASE}/memory/embed", json={"entry_type": "lessons", "entry_id": note})
    assert one.status_code == 200 and one.json() == {"embedded": 1, "model": "test:v1"}
    assert [row["embedded"] for row in client.get(f"{BASE}/memory/lessons").json()].count(True) == 1

    every = client.post(f"{BASE}/memory/embed", json={"entry_type": "lessons"})
    assert every.json()["embedded"] == 2
    assert all(row["embedded"] for row in client.get(f"{BASE}/memory/lessons").json())
