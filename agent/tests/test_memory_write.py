"""Experience memory, write path: redaction, keys, the store, embeddings and the learner."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fakes import config

from vilagent.memory import keys, redact
from vilagent.memory.embeddings import Embedder, pack, reembed, similarity
from vilagent.memory.learn import MAX_INSTRUCTION_CHARS, MIN_STEP_CHARS, NOTE_CHARS, Learner, foreign_subjects, is_verified, needs_lessons, notes_input, worth_notes
from vilagent.memory.store import Episode, MemoryStore
from vilagent.runs.manager import RunManager
from vilagent.runs.records import RunRecords
from vilagent.runs.session import RunSession
from vilagent.server.activity import new_activity

# --- redaction and keys ---------------------------------------------------------


@pytest.mark.parametrize(("raw", "masked"), [
    ('Email "Quarterly numbers" to bob@example.com', "Email «text» to «email»"),
    ("Call +90 532 123 45 67 or 4111-1111-1111-1111", "Call +«number» or «number»"),
    ("Open https://shop.example.com/cart?session=abc#top now", "Open https://shop.example.com/cart now"),
    ("Type 'my secret' and don't stop", "Type «text» and don't stop"),
    ("Save it as note.txt at 10:30", "Save it as note.txt at 10:30"),
])
def test_redaction_masks_personal_data(raw, masked):
    assert redact.text(raw) == masked


def test_step_outline_never_keeps_arguments():
    step = {"instruction": 'Type "hunter2" into the box', "action_kind": "type_text", "environment": "native", "args": {"text": "hunter2"}}

    assert redact.step_outline(step) == {"instruction": "Type «text» into the box", "kind": "type_text", "environment": "native"}


def test_keys_come_from_launches_visits_and_the_prompt():
    plan = {"steps": [{"args": {"app_name": " Notepad "}}, {"args": {"url": "https://www.Mail.google.com/u/0"}}]}

    assert keys.extract("Then check example.org and https://docs.python.org/3/", plan) == (["notepad"], ["docs.python.org", "example.org", "mail.google.com"])
    assert keys.extract("Write to ann@example.com", None) == ([], [])  # an e-mail domain is not a visited site


# --- store and embeddings -----------------------------------------------------------


class FakeEmbeddings:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    async def aembed_documents(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        return [[1.0, float(len(text) % 7), 0.5] for text in texts]


def _store(tmp_path: Path) -> MemoryStore:
    return asyncio.run(MemoryStore.open(tmp_path / "memory.sqlite"))


def test_schema_keyword_search_and_reopen(tmp_path):
    async def scenario():
        store = await MemoryStore.open(tmp_path / "memory.sqlite")
        episode = Episode(run_id="r1", task_text="Write an email in Gmail", approach="plan_execute", plan_outline=[{"instruction": "Compose"}], domains=["mail.google.com"])
        episode_id = await store.add_episode(episode)
        found = await store.search_text("episodes", "compose email")
        await store.close()
        reopened = await MemoryStore.open(tmp_path / "memory.sqlite")  # migrations don't run twice
        count = len(await reopened.episodes())
        await reopened.close()
        return episode_id, found, count

    episode_id, found, count = asyncio.run(scenario())

    assert found == [episode_id] and count == 1


def test_a_known_lesson_is_merged_not_duplicated(tmp_path):
    async def scenario():
        store = await MemoryStore.open(tmp_path / "memory.sqlite")
        first = await store.add_lesson("domain", "mail.google.com", "Press Enter to accept the recipient suggestion.", source="auto")
        again = await store.add_lesson("domain", "mail.google.com", "Press Enter to accept the recipient suggestion first!", source="auto")
        other = await store.add_lesson("domain", "mail.google.com", "Close the cookie banner first.", source="auto")
        rows = await store.lessons(key_type="domain", key="mail.google.com")
        await store.close()
        return first, again, other, rows

    first, again, other, rows = asyncio.run(scenario())

    assert (first[0], again, other[0]) == ("inserted", ("merged", first[1]), "inserted")
    assert sorted(row["hits"] for row in rows) == [1, 2]
    assert any(row["text"].endswith("!") for row in rows)  # the longer wording is kept


def test_vectors_are_normalized_and_rows_get_them_later(tmp_path):
    assert similarity(pack([3.0, 4.0]), pack([6.0, 8.0])) == pytest.approx(1.0)

    async def scenario():
        store = await MemoryStore.open(tmp_path / "memory.sqlite")
        failing = Embedder(FakeEmbeddings(fail=True), "fake:v1")
        vectors = await failing.embed(["hello"])  # provider down: stored without a vector
        await store.add_lesson("app", "notepad", "Use Ctrl+S to save.", source="auto", embedding=None)
        embedded = await reembed(store, Embedder(FakeEmbeddings(), "fake:v2"))
        rows = await store.lessons()
        await store.close()
        return vectors, embedded, rows

    vectors, embedded, rows = asyncio.run(scenario())

    assert vectors is None and embedded == 1
    assert rows[0]["embedding_model"] == "fake:v2" and rows[0]["embedding"] is not None


# --- the learner ---------------------------------------------------------------------


def _session(status: str, steps: list[dict], *, replans: int = 0, prompt: str = 'Send "Hi Ann" to ann@example.com in Gmail') -> RunSession:
    activity = new_activity("t", "r1", prompt, "model", config())
    session = RunSession(run_id="r1", thread_id="t", prompt=prompt, approach="plan_execute", execution_mode="hybrid", activity=activity, status=status)
    plan = {"goal": prompt, "steps": [{"step_id": "s1", "instruction": 'Type "Hi Ann"', "environment": "browser", "action_kind": "type_text", "args": {"text": "Hi Ann", "url": "https://mail.google.com"}}]}
    session.output = {"plan": plan, "steps": steps, "replan_count": replans, "narration": [{"role": "vision", "text": "Typing 'Hi Ann' now."}]}
    session.ended_at = session.started_at
    return session


def test_episode_and_lesson_rules():
    verified = [{"step_id": "s1", "status": "completed", "verified_by": "fara_verify"}]
    unchecked = [{"step_id": "s1", "status": "completed", "verified_by": "none"}]
    deterministic = [{"step_id": "s1", "status": "completed", "verified_by": None}]

    assert is_verified(verified) and is_verified(deterministic) and not is_verified(unchecked)
    assert needs_lessons([{"status": "blocked"}], {}) and needs_lessons([], {"replan_count": 1})
    assert not needs_lessons([{"status": "completed"}], {})


def test_a_run_nobody_checked_is_remembered_but_marked(tmp_path):
    store = _store(tmp_path)

    async def distill(payload):
        return []

    learner = Learner(store, embedder=lambda: None, distill=distill, enabled=lambda: True)
    unchecked = _session("completed", [{"step_id": "s1", "status": "completed", "verified_by": "none"}])
    checked = _session("completed", [{"step_id": "s1", "status": "completed", "verified_by": "fara_verify"}])
    checked.run_id = "r2"

    async def scenario():
        await learner.after_run(unchecked)
        await learner.after_run(checked)
        return await store.episodes()

    episodes = asyncio.run(scenario())
    asyncio.run(store.close())

    assert sorted((row["run_id"], row["verified"]) for row in episodes) == [("r1", 0), ("r2", 1)]


def test_a_successful_run_is_read_back_step_by_step_within_the_budget():
    plan = {"goal": "g", "steps": [{"step_id": f"s{i}", "instruction": f"step {i} " + "i" * 400, "environment": "native"} for i in range(8)]}
    session = _session("completed", [])
    session.output = {
        "plan": plan,
        "steps": [
            {"step_id": f"s{i}", "status": "completed", "verified_by": "none", "actions": 3, "notes": [f"note {j} of step {i} " + "y" * 200 for j in range(12)]}
            for i in range(8)
        ],
    }

    payload = notes_input(session, plan, session.output["steps"], ["step 1: finished without a check"])
    steps = payload["steps"]

    # Every step is there, each with what it was told and what the vision model said doing it.
    assert len(steps) == 8 and payload["verified"] is False and payload["why"] == ["step 1: finished without a check"]
    assert steps[0]["instruction"].startswith("step 0") and steps[0]["vision_notes"].startswith("note 0 of step 0")
    # An equal share each, and a shortened step says so.
    share = max(MIN_STEP_CHARS, NOTE_CHARS // 8)
    assert all(len(step["vision_notes"]) <= share and len(step["instruction"]) <= MAX_INSTRUCTION_CHARS for step in steps)
    assert all(step["cropped"] is True for step in steps)
    assert sum(len(step["vision_notes"]) for step in steps) <= NOTE_CHARS  # the steps share one budget

    short = _session("completed", [{"step_id": "s1", "status": "completed", "verified_by": "fara_finish", "actions": 1, "notes": ["typed it"]}])
    assert notes_input(short, short.output["plan"], short.output["steps"], [])["steps"][0] == {
        "instruction": "Type «text»",
        "actions": 1,
        "vision_notes": "typed it",
    }  # nothing cropped, so nothing said about cropping
    # Only successful runs are read back this way.
    assert notes_input(_session("failed", []), plan, [], ["why"]) is None


def test_learning_stores_masked_episodes_and_lessons(tmp_path):
    store = _store(tmp_path)
    seen = []

    async def distill(payload):
        seen.append(payload)
        return [
            {"key_type": "domain", "key": "https://mail.google.com/", "lesson": "Press Enter after the recipient to accept the suggestion."},
            {"key_type": "planet", "key": "mars", "lesson": "ignored"},
        ]

    learner = Learner(store, embedder=lambda: Embedder(FakeEmbeddings(), "fake:v1"), distill=distill, enabled=lambda: True)
    success = _session("completed", [{"step_id": "s1", "status": "completed", "verified_by": "fara_finish"}])
    # One planner call per run: the trouble for a run that struggled, the notes for one that worked.
    trouble = _session("failed", [{"step_id": "s1", "status": "failed", "error_code": "x", "evidence": {"last_thoughts": ["Typing 'Hi Ann'"]}}], replans=1)

    async def scenario():
        await learner.after_run(success)
        await learner.after_run(trouble)
        return await store.episodes(), await store.lessons()

    episodes, lessons = asyncio.run(scenario())
    asyncio.run(store.close())

    assert len(episodes) == 1
    assert episodes[0]["task_text"] == "Send «text» to «email» in Gmail" and episodes[0]["domains"] == ["mail.google.com"]
    assert episodes[0]["plan_outline"] == [{"instruction": "Type «text»", "kind": "type_text", "environment": "browser"}]
    assert [(row["key_type"], row["key"], row["source"]) for row in lessons] == [("domain", "mail.google.com", "auto")]
    assert [payload.get("problems") is not None for payload in seen] == [False, True] and len(seen) == 2
    assert "Hi Ann" not in str(seen) and "ann@example.com" not in str(seen)  # the lesson model sees masked data only


def test_learning_can_be_off_and_never_raises(tmp_path):
    store = _store(tmp_path)

    async def broken(payload):
        raise RuntimeError("model down")

    trouble = _session("failed", [{"step_id": "s1", "status": "failed"}])
    asyncio.run(Learner(store, embedder=lambda: None, distill=broken, enabled=lambda: True).after_run(trouble))  # logged, not raised
    asyncio.run(Learner(store, embedder=lambda: None, distill=broken, enabled=lambda: False).after_run(_session("completed", [{"step_id": "s1", "status": "completed"}])))

    assert asyncio.run(store.episodes()) == [] and asyncio.run(store.lessons()) == []
    asyncio.run(store.close())


def test_learning_runs_after_the_run_in_the_background(tmp_path):
    manager = RunManager(RunRecords(tmp_path))
    learned = []

    async def after_run(session):
        learned.append((session.run_id, session.status, manager.active))

    manager.after_run = after_run

    async def scenario():
        async def done():
            manager.finish(manager.active, "completed")

        await manager.start(_session("running", []), done)
        await asyncio.gather(*manager._background)

    asyncio.run(scenario())

    assert learned == [("r1", "completed", None)]  # the run slot was already free


def test_only_a_run_worth_a_look_costs_a_planner_call():
    plan = {"goal": "g", "steps": [{"step_id": "s1", "instruction": "Search YouTube for the song", "max_actions": 8}]}
    clean = [{"step_id": "s1", "status": "completed", "verified_by": "fara_verify", "actions": 3, "notes": ["Clicking the search bar.", "Typing the song name.", "Pressing play."]}]
    session = _session("completed", clean, prompt="play the song on youtube")

    # A checked run that kept to its plan, in a place memory knows: nothing to ask the planner.
    assert worth_notes(session, plan, clean, known_keys={"youtube"}) == []
    assert worth_notes(session, plan, clean, known_keys=set()) == ["nothing is remembered about this app or site yet"]

    unchecked = [{**clean[0], "verified_by": "none"}]
    spent = [{**clean[0], "actions": 8, "struggles": ["ran out of its action budget"]}]
    struggled = [{**clean[0], "struggles": ["repeated itself and had to be nudged"]}]
    assert worth_notes(session, plan, unchecked, known_keys={"youtube"}) == ["step 1: finished without a check"]
    assert worth_notes(session, plan, spent, known_keys={"youtube"}) == ["step 1: the vision model ran out of its action budget"]
    assert worth_notes(session, plan, struggled, known_keys={"youtube"}) == ["step 1: the vision model repeated itself and had to be nudged"]


def test_a_step_that_wandered_off_the_task_is_noticed():
    wandered = ["Searching for the channel The Verge.", "Opening the channel page of The Verge.", "The Verge channel has a Videos tab.", "Saving the video to Watch Later.", "Watch Later needs a sign-in."]
    on_task = ["Clicking the search bar.", "Typing the song name.", "The results are loading.", "Clicking the first result.", "Pressing play to start the song."]

    assert foreign_subjects("play sweet disposition on youtube", "search and play it", on_task) == []
    # It has to keep coming back to the same thing; one stray word is not a detour.
    assert "verge" in foreign_subjects("play sweet disposition on youtube", "search and play it", wandered)
    reasons = worth_notes(
        _session("completed", [], prompt="play sweet disposition on youtube"),
        {"steps": [{"step_id": "s1", "instruction": "search and play it", "max_actions": 8}]},
        [{"step_id": "s1", "status": "completed", "verified_by": "fara_verify", "actions": 3, "notes": wandered}],
        known_keys={"youtube"},
    )
    assert reasons and "which the task never mentions" in reasons[0]
