"""Recall: what memory offers a new task. Runs once per run, before planning.

- **Similar runs:** keyword (FTS5 bm25) and vector search over episodes, fused with Reciprocal
  Rank Fusion; near-duplicate tasks are dropped; the top 3 are kept.
- **Lessons:** first everything filed under the apps and domains the task names (operator notes
  first), then "general" lessons that match the task. At most 5.

What each consumer gets is small and capped: the planner sees similar runs and lessons (about
1,500 tokens at most); FARA sees at most 2 lessons for its step (FARA-7B's context is small); the
recovery supervisor sees the same lessons as FARA.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from vilagent.memory import keys
from vilagent.memory.embeddings import Embedder, similarity
from vilagent.memory.store import MemoryStore

RRF_K = 60
CANDIDATES = 20
MAX_RUNS = 3
MAX_LESSONS = 5
NEAR_DUPLICATE = 0.95
PLANNER_CHARS = 6000  # ≈ 1,500 tokens
OUTLINE_STEPS = 8
FARA_LESSONS = 2
FARA_CHARS = 300


@dataclass
class Experience:
    """Recalled memory, JSON-safe so it can live in the run graph's state."""

    similar_runs: list[dict[str, Any]] = field(default_factory=list)  # {"task", "plan": [outline]}
    lessons: list[dict[str, Any]] = field(default_factory=list)  # {"key_type", "key", "text", "source"}

    def to_dict(self) -> dict[str, Any]:
        return {"similar_runs": self.similar_runs, "lessons": self.lessons}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Experience:
        data = data or {}
        return cls(list(data.get("similar_runs") or []), list(data.get("lessons") or []))

    def for_planner(self) -> dict[str, Any]:
        """``context["experience"]`` for the planner or the brief; {} when there is nothing."""
        if not self.similar_runs and not self.lessons:
            return {}
        runs = [{"task": run["task"], "plan": [step["instruction"] for step in run["plan"][:OUTLINE_STEPS]]} for run in self.similar_runs]
        payload = {"similar_runs": runs, "lessons": [lesson["text"] for lesson in self.lessons]}
        while runs and len(json.dumps(payload, ensure_ascii=False)) > PLANNER_CHARS:
            runs.pop()  # the lowest-ranked run goes first
        return payload

    def for_step(self, apps: list[str], domains: list[str]) -> list[str]:
        """At most 2 lessons filed under the step's apps or domains, about 300 characters in total."""
        wanted = {("app", key) for key in apps} | {("domain", key) for key in domains}
        picked: list[str] = []
        for lesson in self.lessons:
            if (lesson["key_type"], lesson["key"]) in wanted and len(picked) < FARA_LESSONS:
                if sum(map(len, picked)) + len(lesson["text"]) > FARA_CHARS:
                    break
                picked.append(lesson["text"])
        return picked


def fuse(*rankings: list[str], k: int = RRF_K) -> list[str]:
    """Reciprocal Rank Fusion: an id ranked well by several searches beats one found by a single search."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda item: -scores[item])


async def recall(store: MemoryStore, prompt: str, *, embedder: Embedder | None, run_id: str | None = None) -> Experience:
    vector = model = None
    if embedder is not None and (vectors := await embedder.embed([prompt])):
        vector, model = vectors[0], embedder.model_id

    async def ranked(table: str) -> list[str]:
        by_text = await store.search_text(table, prompt, CANDIDATES)
        if vector is None:
            return by_text
        by_vector = await store.nearest(table, vector, model, CANDIDATES)
        # bm25 matches a single shared word ("open" finds every "open …" task), so a keyword hit
        # the vectors judged unrelated is dropped. Rows this model has no vector for are kept:
        # nothing has judged them yet.
        judged, near = await store.embedded_ids(table, model), set(by_vector)
        return fuse([row_id for row_id in by_text if row_id in near or row_id not in judged], by_vector)

    # Runs nobody checked are kept, but a checked run of the same rank is the better example.
    found = sorted(await store.get("episodes", await ranked("episodes")), key=lambda row: not row.get("verified", 1))
    runs = _distinct(found)[:MAX_RUNS]

    words = set(re.findall(r"[\w.-]+", prompt.lower()))
    apps = [key for key in await store.lesson_keys("app") if set(key.split()) <= words]
    domains = keys.extract(prompt, None)[1]
    lessons = await store.lessons_for_keys(apps, domains)
    general = [row for row in await store.get("lessons", await ranked("lessons")) if row["key_type"] == "general"]
    lessons = _unique(lessons + general)[:MAX_LESSONS]

    if run_id is not None:
        await store.record_usage(run_id, [("episodes", row["id"]) for row in runs] + [("lessons", row["id"]) for row in lessons])
    return Experience(
        similar_runs=[{"task": row["task_text"], "plan": row["plan_outline"]} for row in runs],
        lessons=[{key: row[key] for key in ("key_type", "key", "text", "source")} for row in lessons],
    )


def step_keys(step: dict[str, Any], plan: dict[str, Any], prompt: str) -> tuple[list[str], list[str]]:
    """Where a step works: apps on the desktop, sites in the browser; its own, else the task's."""
    own_apps, own_domains = keys.extract("", {"steps": [step]})
    task_apps, task_domains = keys.extract(prompt, plan)
    if step.get("environment") == "browser":
        return [], own_domains or task_domains
    return own_apps or task_apps, []


def _distinct(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop runs that repeat a higher-ranked one's task."""
    kept: list[dict[str, Any]] = []
    for row in rows:
        if not any(_same_task(row, other) for other in kept):
            kept.append(row)
    return kept


def _same_task(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("embedding") and b.get("embedding") and a.get("embedding_model") == b.get("embedding_model"):
        return similarity(a["embedding"], b["embedding"]) >= NEAR_DUPLICATE
    return difflib.SequenceMatcher(None, a["task_text"].lower(), b["task_text"].lower()).ratio() >= NEAR_DUPLICATE


def _unique(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    return [row for row in rows if not (row["id"] in seen or seen.add(row["id"]))]
