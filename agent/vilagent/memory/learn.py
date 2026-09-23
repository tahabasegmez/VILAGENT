"""Learning from a finished run, in the background (it never changes the run's result).

- **Episode:** the run completed. Memory keeps the masked task, the plan outline and the apps and
  domains it touched, so a similar task can reuse a plan that worked; ``verified`` records whether
  every step was actually checked, and recall prefers the checked ones.
- **Lessons:** at most one planner call per run, and only when the run has something to say.
  A run that struggled (a blocked or failed step, or a replan) is turned into lessons about the
  trouble. A run that **succeeded** is read back step by step — for each step the instruction the
  vision model was given and the notes it wrote while acting — but only when ``worth_notes``
  finds a reason: the loop struggled, a step finished without a check, a step kept coming back to
  something the task never mentions, or memory holds nothing about this app or site yet. A clean
  repeat costs nothing. Either way: at most three short, reusable lessons filed under an app, a
  domain or "general", and known lessons are merged instead of duplicated. The steps share one
  budget (``NOTE_CHARS``), so a long run costs no more context than a short one, and a shortened
  step says ``cropped``.

A declined approval teaches nothing about the UI (it was the operator's choice), so it adds no
lesson.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from vilagent.agents.common import extract_json_object, message_text
from vilagent.memory import keys, redact
from vilagent.memory.embeddings import Embedder
from vilagent.memory.store import Episode, MemoryStore
from vilagent.runs.session import RunSession

logger = logging.getLogger(__name__)

KEY_TYPES = {"app", "domain", "general"}
MAX_LESSONS = 3
MAX_LESSON_CHARS = 200
_TROUBLE = {"blocked", "failed"}
# Reading a successful run back, step by step: the steps share NOTE_CHARS of the vision model's
# notes between them (each keeping at least MIN_STEP_CHARS), and their instructions are counted
# separately because they are short. A step whose notes do not fit is marked cropped.
NOTE_CHARS = 4000
MIN_STEP_CHARS = 200
MAX_INSTRUCTION_CHARS = 160
# Drift: a step whose notes keep coming back to something the task never mentions (a channel
# nobody asked for, a sign-in flow). Counted, not judged: a subject has to be named three times
# before it counts, which a passing remark never is. On the maintainer's runs, steps that stayed
# on task named no such subject at all, so one is enough to ask the planner to look.
DRIFT_REPEATS = 3
DRIFT_SUBJECTS = 1
# The words any run is full of; they say nothing about whether it stayed on task.
_ROUTINE = frozenset(
    """click clicks clicked clicking press pressed pressing type typed typing enter select selected open opened opening close closed
    closing scroll scrolled scrolling wait waiting load loads loaded loading page pages screen button buttons field fields box bar
    link links icon menu tab tabs window dialog popup banner overlay result results list item items text input search searching
    task step next then need needs needed should will must current currently first second last again another because since after
    before user users this that these those with from into onto over under about there their them they have has had been being
    action actions model view show shows showing see seen look looking find finds finding make makes made take takes taken
    video videos play playing playback player title titles thumbnail feed home main detail details option options filter filters
    category query queries word words letter site website browser address bar top bottom left right center centre area section
    panel row column place position order still already also just only more most some many much very same other others
    focus focused focusing refocus submit submits submitted submitting ensure ensures ensuring active inactive correct incorrect
    correctly properly desired start starts started starting stop stops clear clears cleared ready available visible hidden empty
    exact proceed proceeds proceeding continue continues complete completes completed confirm confirms attempt attempts trying
    replace replaces replacing retype rewrite again
    song songs track tracks music audio sound volume clip clips movie film episode""".split()
)
_WORDS = re.compile(r"[^\W\d_]+", re.UNICODE)

LESSON_PROMPT = """\
You turn the trouble one computer-use run had into lessons for FUTURE runs on the same app or website.
Output ONLY one JSON object: {"lessons":[{"key_type":"app|domain|general","key":"...","lesson":"..."}]}.
- At most 3 lessons, each ONE imperative sentence of at most 200 characters, in English.
- key_type "app" with the app's name (e.g. "notepad"), "domain" with the site's host (e.g. "mail.google.com"),
  or "general" (key "general") only for advice that holds everywhere.
- A lesson must be concrete and reusable: what to do (or avoid) and where. For example:
  "After typing a recipient, press Enter to accept the highlighted suggestion before moving on."
- Never include personal data, typed text, names, addresses or numbers from the task.
- If the run teaches nothing reusable, return {"lessons":[]}."""

NOTES_PROMPT = """\
A computer-use run succeeded. "task" is WHAT THE OPERATOR ASKED FOR — read it first; it is the
measure for everything below. Each step then gives the instruction the vision model was given and
the notes it wrote while carrying it out: what it was told, and what it actually did.
Check each step against the operator's task and against its own instruction.
Report anything unusual: work that served neither the task nor the step, a detour, a retry, a
control that was not where expected. Write it so the next run avoids it.
Output ONLY one JSON object: {"lessons":[{"key_type":"app|domain|general","key":"...","lesson":"..."}]}.
- At most 3 lessons, each ONE imperative sentence of at most 200 characters, in English.
- key_type "app" with the app's name (e.g. "notepad"), "domain" with the site's host, or "general".
- Only what the notes show. No personal data, typed text, names, addresses or numbers.
- "why" is what already looked odd about this run; start there, and ignore it if the notes disagree.
- "cropped": true means that step's notes were shortened; judge only what you can see.
- Nothing worth passing on: {"lessons":[]}."""

Distill = Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]]]]


class Learner:
    def __init__(self, store: MemoryStore, *, embedder: Callable[[], Embedder | None], distill: Distill, enabled: Callable[[], bool]):
        self._store = store
        self._embedder = embedder
        self._distill = distill
        self._enabled = enabled

    async def after_run(self, session: RunSession) -> None:
        """Learn what the run can teach; errors are logged, never raised."""
        try:
            if self._enabled():
                await self._learn(session)
        except Exception:
            logger.warning("Learning from run %s failed", session.run_id, exc_info=True)

    async def _learn(self, session: RunSession) -> None:
        output = session.output or {}
        plan, steps = output.get("plan") or {}, output.get("steps") or []
        if not plan.get("steps"):
            return
        embedder = self._embedder()
        apps, domains = keys.extract(session.prompt, plan)
        if session.status == "completed" and steps:
            episode = Episode(
                run_id=session.run_id,
                task_text=redact.text(session.prompt),
                approach=session.approach,
                plan_outline=[redact.step_outline(step) for step in plan["steps"]],
                apps=apps,
                domains=domains,
                environments=sorted({str(step.get("environment")) for step in plan["steps"]}),
                actions=sum(int(step.get("actions") or 0) for step in steps),
                duration_s=_duration(session),
                verified=is_verified(steps),
            )
            vectors = await embedder.embed([episode.task_text]) if embedder else None
            await self._store.add_episode(episode, embedding=vectors[0] if vectors else None, model=embedder.model_id if vectors else None)
        # One planner call at most, and only when the run has something to say: the trouble if
        # there was any, otherwise a run that looked odd (worth_notes). A clean repeat costs nothing.
        if needs_lessons(steps, output):
            payload = lesson_input(session, plan, steps, output)
        elif why := worth_notes(session, plan, steps, known_keys=await self._known_keys(apps, domains)):
            payload = notes_input(session, plan, steps, why)
        else:
            payload = None
        if payload:
            lessons = [lesson for item in (await self._distill(payload))[:MAX_LESSONS] if (lesson := _clean(item))]
            vectors = await embedder.embed([text for _, _, text in lessons]) if embedder and lessons else None
            for index, (key_type, key, text) in enumerate(lessons):
                vector = vectors[index] if vectors else None
                await self._store.add_lesson(key_type, key, text, source="auto", from_run_id=session.run_id, embedding=vector, model=embedder.model_id if vector else None)


    async def _known_keys(self, apps: list[str], domains: list[str]) -> set[str]:
        """Which of this run's apps and sites memory already holds lessons for."""
        known = set(await self._store.lesson_keys("app")) | set(await self._store.lesson_keys("domain"))
        return known & (set(apps) | set(domains))


def is_verified(steps: list[dict[str, Any]]) -> bool:
    """Whether every completed step was actually checked (FARA said so, or a step check passed)."""
    return all(step.get("verified_by") != "none" for step in steps if step.get("status") == "completed")


def needs_lessons(steps: list[dict[str, Any]], output: dict[str, Any]) -> bool:
    return any(step.get("status") in _TROUBLE for step in steps) or int(output.get("replan_count") or 0) > 0


def lesson_input(session: RunSession, plan: dict[str, Any], steps: list[dict[str, Any]], output: dict[str, Any]) -> dict[str, Any]:
    """What the lesson model sees: masked task, plan outline, the troubled steps and FARA's last notes."""
    instructions = {step["step_id"]: redact.text(step.get("instruction")) for step in plan["steps"]}
    problems = [
        {
            "step": instructions.get(step.get("step_id"), ""),
            "status": step.get("status"),
            "error_code": step.get("error_code"),
            "summary": redact.text(step.get("summary")),
            "last_notes": [redact.text(note) for note in (step.get("evidence") or {}).get("last_thoughts") or []],
            "where": redact.text((step.get("evidence") or {}).get("context")),
        }
        for step in steps
        if step.get("status") in _TROUBLE
    ]
    notes = [redact.text(item.get("text")) for item in (output.get("narration") or [])[-8:]]
    return {
        "task": redact.text(session.prompt),
        "outcome": session.status,
        "plan": [redact.step_outline(step) for step in plan["steps"]],
        "problems": problems,
        "replans": int(output.get("replan_count") or 0),
        "recent_notes": notes,
    }


def worth_notes(session: RunSession, plan: dict[str, Any], steps: list[dict[str, Any]], *, known_keys: set[str]) -> list[str]:
    """Why this successful run is worth a planner call — empty when it is not.

    A run that followed its plan, finished inside its budget, was checked and stayed on topic in
    a place memory already knows teaches nothing new, so it is not read back at all. Everything
    here is decided from the record: the decision itself costs nothing.
    """
    instructions = {step["step_id"]: str(step.get("instruction") or "") for step in plan.get("steps") or []}
    reasons: list[str] = []
    if not known_keys:
        reasons.append("nothing is remembered about this app or site yet")
    for number, step in enumerate(steps, 1):
        where = f"step {number}"
        for struggle in step.get("struggles") or []:
            reasons.append(f"{where}: the vision model {struggle}")
        if step.get("status") == "completed" and step.get("verified_by") == "none":
            reasons.append(f"{where}: finished without a check")
        if len(strayed := foreign_subjects(session.prompt, instructions.get(step.get("step_id"), ""), step.get("notes") or [])) >= DRIFT_SUBJECTS:
            reasons.append(f"{where}: kept coming back to {', '.join(strayed[:4])}, which the task never mentions")
    return reasons


def foreign_subjects(task: str, instruction: str, notes: list[str]) -> list[str]:
    """Subjects a step kept returning to that neither the task nor its instruction ever mentions.

    Word counts, not meaning: the words a run says while simply operating a screen are ignored,
    so what is left is what the step was about. A step that went after something else keeps
    naming it, which is why a subject has to recur before it counts.
    """
    asked = _subject_words(f"{task} {instruction}")
    counted = Counter(word for line in notes for word in _subject_words(line))
    return sorted(word for word, times in counted.items() if times >= DRIFT_REPEATS and word not in asked)


def _subject_words(text: str) -> set[str]:
    return {word for word in _WORDS.findall(text.lower()) if len(word) > 3 and word not in _ROUTINE}


def notes_input(session: RunSession, plan: dict[str, Any], steps: list[dict[str, Any]], why: list[str]) -> dict[str, Any] | None:
    """A successful run step by step: what the vision model was told, and what it said while acting.

    The planner compares the two, so it gets both for every step. The steps share ``NOTE_CHARS``
    of notes evenly (never below ``MIN_STEP_CHARS`` each), and a step whose notes did not fit says
    so, so the planner knows it is judging a shortened account. Only successful runs come here.
    """
    if session.status != "completed" or not steps:
        return None
    instructions = {step["step_id"]: redact.text(step.get("instruction")) for step in plan["steps"]}
    share = max(MIN_STEP_CHARS, NOTE_CHARS // len(steps))
    return {
        "task": redact.text(session.prompt)[:MAX_INSTRUCTION_CHARS],
        "verified": is_verified(steps),
        "why": [redact.text(reason) for reason in why],
        "steps": [_step_notes(step, instructions, share) for step in steps],
    }


def _step_notes(step: dict[str, Any], instructions: dict[str, str], share: int) -> dict[str, Any]:
    """One step: its instruction, how many actions it took, and the vision model's own notes."""
    notes = " · ".join(redact.text(note) for note in step.get("notes") or [])
    return {
        "instruction": instructions.get(step.get("step_id"), "")[:MAX_INSTRUCTION_CHARS],
        "actions": step.get("actions"),
        "vision_notes": notes[:share],
        **({"cropped": True} if len(notes) > share else {}),
    }


def model_distiller(model_factory: Callable[[], Any]) -> Distill:
    """Lessons from the planner model (its call is not charged to the finished run's budget)."""

    async def distill(payload: dict[str, Any]) -> list[dict[str, Any]]:
        prompt = LESSON_PROMPT if payload.get("problems") is not None else NOTES_PROMPT
        response = await model_factory().ainvoke([SystemMessage(content=prompt), HumanMessage(content=json.dumps(payload, ensure_ascii=False))])
        lessons = extract_json_object(message_text(response)).get("lessons")
        return lessons if isinstance(lessons, list) else []

    return distill


def _clean(item: Any) -> tuple[str, str, str] | None:
    if not isinstance(item, dict):
        return None
    key_type = str(item.get("key_type") or "").strip().lower()
    raw_key = str(item.get("key") or "").strip()
    key = "general" if key_type == "general" else keys.domain_key(raw_key) if key_type == "domain" else keys.app_key(raw_key)
    text = redact.text(" ".join(str(item.get("lesson") or "").split()))[:MAX_LESSON_CHARS]
    if key_type not in KEY_TYPES or not key or not text:
        return None
    return key_type, key, text


def _duration(session: RunSession) -> float | None:
    try:
        return round((datetime.fromisoformat(session.ended_at or "") - datetime.fromisoformat(session.started_at)).total_seconds(), 1)
    except ValueError:
        return None
