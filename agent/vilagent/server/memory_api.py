"""The experience-memory API behind the memory panel (under ``/api/computer-use``)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from vilagent.memory import keys
from vilagent.memory.embeddings import embed_entries
from vilagent.memory.retrieval import fuse
from vilagent.memory.store import MemoryStore, public
from vilagent.server.deps import get_runtime, require_internal_request
from vilagent.server.memory import current_embedder
from vilagent.server.runtime import Runtime
from vilagent.server.state import get_state_value, set_state_value

router = APIRouter(prefix="/api/computer-use", tags=["memory"], dependencies=[Depends(require_internal_request)])

ENTRY_ID = r"^[a-f0-9]{32}$"
RUN_ID = r"^[A-Za-z0-9._-]{1,200}$"
NOTE_CHARS = 500
KeyType = Literal["app", "domain", "general"]


class MemorySelection(BaseModel):
    enabled: bool


class Rating(BaseModel):
    rating: Literal["good", "bad"] | None
    model_config = ConfigDict(extra="forbid")


class NewNote(BaseModel):
    key_type: KeyType
    key: str = Field(default="general", max_length=200)
    text: str = Field(min_length=1, max_length=NOTE_CHARS)
    model_config = ConfigDict(extra="forbid")


class LessonEdit(BaseModel):
    key_type: KeyType | None = None
    key: str | None = Field(default=None, max_length=200)
    text: str | None = Field(default=None, min_length=1, max_length=NOTE_CHARS)
    disabled: bool | None = None
    model_config = ConfigDict(extra="forbid")


class ClearRequest(BaseModel):
    confirm: Literal[True]


class EmbedRequest(BaseModel):
    """Which rows to embed: one entry, or every row of that table."""

    entry_type: Literal["episodes", "lessons"]
    entry_id: str | None = Field(default=None, pattern=ENTRY_ID)
    model_config = ConfigDict(extra="forbid")


def get_memory(runtime: Runtime = Depends(get_runtime)) -> MemoryStore:
    if runtime.memory is None:
        raise HTTPException(status_code=503, detail="Experience memory is not open.")
    return runtime.memory


@router.get("/memory/enabled", response_model=MemorySelection, summary="Is experience memory on?")
async def get_memory_enabled() -> MemorySelection:
    return MemorySelection(enabled=bool(get_state_value("memory_enabled", True)))


@router.post("/memory/enabled", response_model=MemorySelection, summary="Turn experience memory on or off (learning and recall)")
async def set_memory_enabled(body: MemorySelection) -> MemorySelection:
    set_state_value("memory_enabled", body.enabled)
    return body


@router.get("/memory/search", summary="Search remembered runs and lessons (keyword + meaning)")
async def search(q: str = Query(min_length=1, max_length=500), type: Literal["episodes", "lessons", "all"] = "all", store: MemoryStore = Depends(get_memory)) -> dict:
    embedder = current_embedder()
    vectors = await embedder.embed([q]) if embedder else None
    found: dict[str, list[dict]] = {}
    for table in ("episodes", "lessons") if type == "all" else (type,):
        by_vector = await store.nearest(table, vectors[0], embedder.model_id) if vectors else []
        ids = fuse(await store.search_text(table, q), by_vector)
        found[table] = [public(row) for row in await store.get(table, ids, usable=False)]
    return found


@router.post("/memory/embed", summary="Embed an entry, or a whole table, with the current model")
async def embed_memory(body: EmbedRequest, store: MemoryStore = Depends(get_memory)) -> dict:
    embedder = current_embedder()
    if embedder is None:
        raise HTTPException(status_code=409, detail="Memory search is on keywords only. Point it at an embedding connection first.")
    embedded = await embed_entries(store, embedder, body.entry_type, [body.entry_id] if body.entry_id else None)
    return {"embedded": embedded, "model": embedder.model_id}


@router.get("/memory/episodes", summary="Remembered runs, newest first")
async def list_episodes(store: MemoryStore = Depends(get_memory)) -> list[dict]:
    return [public(row) for row in await store.episodes()]


@router.patch("/memory/episodes/{entry_id}", summary="Rate a remembered run (a bad one is never recalled)")
async def rate_episode(body: Rating, entry_id: str = PathParam(pattern=ENTRY_ID), store: MemoryStore = Depends(get_memory)) -> dict:
    if not await store.update("episodes", entry_id, rating=body.rating):
        raise HTTPException(status_code=404, detail="Unknown entry.")
    return {"id": entry_id, "rating": body.rating}


@router.delete("/memory/episodes/{entry_id}", summary="Forget a remembered run")
async def delete_episode(entry_id: str = PathParam(pattern=ENTRY_ID), store: MemoryStore = Depends(get_memory)) -> dict:
    return await _delete(store, "episodes", entry_id)


@router.get("/memory/lessons", summary="Lessons and operator notes")
async def list_lessons(store: MemoryStore = Depends(get_memory)) -> list[dict]:
    return [public(row) for row in await store.lessons()]


@router.post("/memory/lessons", status_code=201, summary="Add an operator note (ranked before learned lessons)")
async def add_note(body: NewNote, store: MemoryStore = Depends(get_memory)) -> dict:
    key = _key(body.key_type, body.key)
    text = " ".join(body.text.split())
    embedder = current_embedder()
    vectors = await embedder.embed([text]) if embedder else None
    _, lesson_id = await store.add_lesson(body.key_type, key, text, source="operator", embedding=vectors[0] if vectors else None, model=embedder.model_id if vectors else None)
    return {"id": lesson_id, "key_type": body.key_type, "key": key}


@router.patch("/memory/lessons/{entry_id}", summary="Edit, switch off or on, or re-file a lesson")
async def edit_lesson(body: LessonEdit, entry_id: str = PathParam(pattern=ENTRY_ID), store: MemoryStore = Depends(get_memory)) -> dict:
    fields = body.model_dump(exclude_none=True)
    if "key_type" in fields or "key" in fields:
        (current,) = await store.get("lessons", [entry_id], usable=False) or [None]
        if current is None:
            raise HTTPException(status_code=404, detail="Unknown entry.")
        key_type = fields.get("key_type", current["key_type"])
        fields |= {"key_type": key_type, "key": _key(key_type, fields.get("key", current["key"]))}
    if "text" in fields:
        fields["text"] = " ".join(fields["text"].split())
    if "disabled" in fields:
        fields["disabled"] = int(fields["disabled"])
    if not fields:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    if not await store.update("lessons", entry_id, **fields):
        raise HTTPException(status_code=404, detail="Unknown entry.")
    return {"id": entry_id, **fields}


@router.delete("/memory/lessons/{entry_id}", summary="Delete a lesson or note")
async def delete_lesson(entry_id: str = PathParam(pattern=ENTRY_ID), store: MemoryStore = Depends(get_memory)) -> dict:
    return await _delete(store, "lessons", entry_id)


@router.get("/runs/{run_id}/memory", summary="What memory a run was given")
async def run_memory(run_id: str = PathParam(pattern=RUN_ID), store: MemoryStore = Depends(get_memory)) -> dict:
    used = await store.used_by(run_id)
    return {table: [public(row) for row in rows] for table, rows in used.items()}


@router.post("/runs/{run_id}/rating", summary="Rate a finished run (its remembered example)")
async def rate_run(body: Rating, run_id: str = PathParam(pattern=RUN_ID), store: MemoryStore = Depends(get_memory)) -> dict:
    if not await store.rate_run(run_id, body.rating):
        raise HTTPException(status_code=404, detail="This run was not kept as an example (only completed runs are).")
    return {"run_id": run_id, "rating": body.rating}


@router.post("/memory/clear", summary="Forget everything in memory")
async def clear(body: ClearRequest, store: MemoryStore = Depends(get_memory)) -> dict:
    await store.clear()
    return {"cleared": True}


def _key(key_type: str, raw: str) -> str:
    key = "general" if key_type == "general" else keys.domain_key(raw) if key_type == "domain" else keys.app_key(raw)
    if not key:
        raise HTTPException(status_code=422, detail="Say which app or site this is about.")
    return key


async def _delete(store: MemoryStore, table: str, entry_id: str) -> dict:
    if not await store.delete(table, entry_id):
        raise HTTPException(status_code=404, detail="Unknown entry.")
    return {"id": entry_id, "deleted": True}
