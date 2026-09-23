"""Experience memory, read path: recall, fusion, caps and what each consumer is given."""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeEnv, FakeFara, click, finish, run_graph

from vilagent.agents.common import Plan, PlanStep
from vilagent.agents.plan_execute import vision_step_command
from vilagent.agents.supervisor import RecoverySupervisor
from vilagent.memory.embeddings import Embedder
from vilagent.memory.retrieval import Experience, fuse, recall, step_keys
from vilagent.memory.store import Episode, MemoryStore


def test_fusion_prefers_what_both_searches_found():
    assert fuse(["a", "b", "c"], ["c", "d"]) == ["c", "a", "b", "d"]
    assert fuse(["a"], []) == ["a"]


class TopicEmbeddings:
    """Mail-ish texts point one way, everything else another."""

    async def aembed_documents(self, texts):
        return [[1.0, 0.0] if "mail" in text.lower() or "message" in text.lower() else [0.0, 1.0] for text in texts]


async def _seed(store: MemoryStore, embedder: Embedder) -> dict[str, str]:
    ids = {}

    async def episode(name, task, rating=None):
        vector = (await embedder.embed([task]))[0]
        ids[name] = await store.add_episode(Episode(run_id=name, task_text=task, approach="plan_execute", plan_outline=[{"instruction": f"{name} plan"}]), embedding=vector, model=embedder.model_id)
        if rating:
            await store.update("episodes", ids[name], rating=rating)

    await episode("gmail", "Send an email to «email» in Gmail")
    await episode("gmail-again", "Send an email to «email» in Gmail!")  # a near-duplicate task
    await episode("outlook", "Write a message to the team in Outlook")
    await episode("bad", "Send an email to «email» in Gmail quickly", rating="bad")
    await episode("paint", "Draw a circle in Paint")
    for key_type, key, text, source in [
        ("domain", "mail.google.com", "Press Enter to accept the recipient suggestion.", "auto"),
        ("domain", "mail.google.com", "Compose is the button at the top left.", "operator"),
        ("app", "notepad", "Use Ctrl+S to save.", "auto"),
        ("general", "general", "Close cookie banners before sending mail.", "auto"),
    ]:
        _, ids[text] = await store.add_lesson(key_type, key, text, source=source)
    _, ids["off"] = await store.add_lesson("domain", "mail.google.com", "Old advice.", source="auto")
    await store.update("lessons", ids["off"], disabled=1)
    return ids


def test_recall_fuses_filters_and_records_usage(tmp_path):
    async def scenario():
        store = await MemoryStore.open(tmp_path / "memory.sqlite")
        embedder = Embedder(TopicEmbeddings(), "topic:v1")
        ids = await _seed(store, embedder)
        experience = await recall(store, "Send a mail to Ann via https://mail.google.com", embedder=embedder, run_id="r9")
        cursor = await store._db.execute("SELECT entry_type, entry_id FROM memory_usage WHERE run_id = 'r9' ORDER BY rank")
        usage = [tuple(row) for row in await cursor.fetchall()]
        uses = {row["id"]: row["uses"] for row in await store.episodes()}
        await store.close()
        return ids, experience, usage, uses

    ids, experience, usage, uses = asyncio.run(scenario())

    tasks = [run["task"] for run in experience.similar_runs]
    gmail = [task for task in tasks if task.startswith("Send an email to «email» in Gmail") and "quickly" not in task]
    assert len(gmail) == 1 and tasks[0] == gmail[0]  # the near-duplicate is dropped (the two tie)
    assert all("quickly" not in task for task in tasks)  # rated bad
    texts = [lesson["text"] for lesson in experience.lessons]
    assert texts[:2] == ["Compose is the button at the top left.", "Press Enter to accept the recipient suggestion."]  # operator note first
    assert "Old advice." not in texts and "Use Ctrl+S to save." not in texts
    assert "Close cookie banners before sending mail." in texts  # a matching general lesson
    kept = ids["gmail"] if gmail[0].endswith("Gmail") else ids["gmail-again"]
    assert usage[0] == ("episodes", kept) and uses[kept] == 1


def test_an_unrelated_run_is_not_offered_as_an_example(tmp_path):
    """A shared word is not relevance: "open" must not make an old task an example for a new one."""

    async def scenario():
        store = await MemoryStore.open(tmp_path / "memory.sqlite")
        embedder = Embedder(TopicEmbeddings(), "topic:v1")

        async def episode(run_id, task, embedded=True):
            vector = (await embedder.embed([task]))[0] if embedded else None
            await store.add_episode(
                Episode(run_id=run_id, task_text=task, approach="plan_execute", plan_outline=[{"instruction": task}]),
                embedding=vector,
                model=embedder.model_id if embedded else None,
            )

        await episode("mail", "Open the mail message from Ann")
        await episode("folder", "Open the documents folder")  # shares "open", points elsewhere
        await episode("unjudged", "Open the message archive", embedded=False)  # no vector yet
        experience = await recall(store, "Open the mail message from Bea", embedder=embedder)
        await store.close()
        return [run["task"] for run in experience.similar_runs]

    tasks = asyncio.run(scenario())

    assert "Open the mail message from Ann" in tasks
    assert "Open the documents folder" not in tasks  # the vectors say it is unrelated
    assert "Open the message archive" in tasks  # nothing has judged it, so the keyword hit stands


def test_recall_works_without_embeddings(tmp_path):
    async def scenario():
        store = await MemoryStore.open(tmp_path / "memory.sqlite")
        await store.add_episode(Episode(run_id="r", task_text="Draw a circle in Paint", approach="plan_execute", plan_outline=[{"instruction": "Draw"}]))
        experience = await recall(store, "draw a red circle in paint", embedder=None)
        await store.close()
        return experience

    assert [run["task"] for run in asyncio.run(scenario()).similar_runs] == ["Draw a circle in Paint"]


def test_caps_for_the_planner_and_for_fara():
    runs = [{"task": f"task {i}", "plan": [{"instruction": "x" * 400}] * 20} for i in range(3)]
    lessons = [{"key_type": "app", "key": "notepad", "text": f"lesson {i} " + "y" * 150, "source": "auto"} for i in range(5)]
    experience = Experience(runs, lessons)

    advice = experience.for_planner()
    assert len(str(advice)) <= 7000 and all(len(run["plan"]) <= 8 for run in advice["similar_runs"])
    assert len(experience.for_step(["notepad"], [])) == 1  # two would exceed 300 characters
    assert experience.for_step(["paint"], []) == [] and Experience().for_planner() == {}


def test_steps_get_lessons_for_where_they_work():
    plan = {"steps": [{"args": {"app_name": "Notepad"}}, {"args": {"url": "https://mail.google.com"}}]}

    assert step_keys({"environment": "native", "args": {}}, plan, "") == (["notepad"], [])
    assert step_keys({"environment": "browser", "args": {}}, plan, "") == ([], ["mail.google.com"])
    assert step_keys({"environment": "browser", "args": {"url": "https://docs.python.org"}}, plan, "") == ([], ["docs.python.org"])


def test_lessons_reach_fara_and_the_supervisor():
    step = PlanStep(step_id="s1", instruction="save the file", risk={"level": "low"})
    assert "LESSONS FROM EARLIER RUNS HERE: Use Ctrl+S to save." in vision_step_command(step, max_actions=8, lessons=["Use Ctrl+S to save."])
    assert "LESSONS" not in vision_step_command(step, max_actions=8)

    seen = []

    class Model:
        async def ainvoke(self, messages):
            seen.append(messages[-1].content[0]["text"])
            return type("Msg", (), {"content": "PROCEED"})()

    asyncio.run(RecoverySupervisor(lambda: Model()).advise(goal="g", done_when="d", thought=None, image_base64="", media_type="image/png", lessons=["Close the popup."]))
    assert "Close the popup." in seen[0]


class Planner:
    def __init__(self, steps):
        self.contexts = []
        self._plan = Plan(goal="g", steps=steps)

    async def plan(self, prompt, *, context):
        self.contexts.append(context)
        return self._plan

    async def replan(self, prompt, **kwargs):
        return self._plan


@pytest.mark.parametrize("remembers", [True, False])
def test_the_graph_recalls_once_and_hands_it_out(remembers):
    recalled = []
    experience = {"similar_runs": [{"task": "Save a note", "plan": [{"instruction": "Type and save"}]}], "lessons": [{"key_type": "app", "key": "notepad", "text": "Use Ctrl+S to save.", "source": "auto"}]}

    async def recall_fn(prompt):
        recalled.append(prompt)
        return experience

    steps = [
        PlanStep(step_id="s1", instruction="launch notepad", requires_vision=False, action_kind="launch_app", args={"app_name": "Notepad"}, risk={"level": "low"}),
        PlanStep(step_id="s2", instruction="fill in the note and click save", risk={"level": "low"}),
    ]
    planner, fara = Planner(steps), FakeFara([click(), finish()])

    result = run_graph(desktop=FakeEnv(), browser=FakeEnv("browser"), fara=fara, planner=planner, prompt="Write a note", recall=recall_fn if remembers else None)

    assert result.status.value == "completed"
    if remembers:
        assert recalled == ["Write a note"]
        assert planner.contexts[0]["experience"]["lessons"] == ["Use Ctrl+S to save."]
        assert "Use Ctrl+S to save." in fara.calls[0]["instruction"]
    else:
        assert "experience" not in planner.contexts[0]
        assert "LESSONS" not in fara.calls[0]["instruction"]
