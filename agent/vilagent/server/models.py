"""Which connection plays which role, and whether it answers.

Each role points at one connection (``vilagent/connections.py``), stored as ``<role>_connection``
in the state file. There is nothing else to configure: how a model behaves — its temperature, its
reply cap, its timeout — belongs to the connection, under LangChain's own argument names.

- ``planner``     an ``llm`` or ``vlm`` connection: the plan, the brief and the lessons;
- ``supervisor``  a ``vlm`` connection (it is shown screenshots), or ``"planner"`` to follow it;
- ``vision``      a ``computer_use`` connection: the model that acts on the screen;
- ``embeddings``  an ``embedding`` connection, or "" for memory search by keyword only.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from vilagent import connections
from vilagent.agents.common import message_text
from vilagent.config.app_config import AppConfig
from vilagent.server.state import get_state_value

CHECK_TIMEOUT = 30.0
SAME_AS_PLANNER = "planner"
ROLES = ("planner", "supervisor", "vision", "embeddings")
#: Which types of connection each role accepts.
ROLE_KINDS: dict[str, tuple[str, ...]] = {
    "planner": (connections.LLM, connections.VLM),
    "supervisor": (connections.VLM,),
    "vision": (connections.COMPUTER_USE,),
    "embeddings": (connections.EMBEDDING,),
}
ChatFactory = Callable[[bool], BaseChatModel]


class Choice(BaseModel):
    """What a role points at: a connection id, ``"planner"``, or "" for nothing."""

    connection: str = ""


class ModelInfo(BaseModel):
    name: str
    label: str
    model: str | None = None
    where: str  # the host it talks to, or the provider package
    sees_images: bool = False


class Check(BaseModel):
    role: str
    name: str
    model: str | None = None
    where: str
    ok: bool
    latency_ms: int | None = None
    detail: str


# --- choosing ---------------------------------------------------------------------


def choice(role: str) -> Choice:
    """The connection this role points at, if it still exists."""
    stored = str(get_state_value(f"{role}_connection", "") or "")
    if role == "supervisor" and stored == SAME_AS_PLANNER:
        return Choice(connection=SAME_AS_PLANNER)
    return Choice(connection=stored) if stored and connections.get(stored) else Choice()


def connection_for(role: str) -> connections.Connection | None:
    """The connection a role runs on; None when it points at nothing."""
    picked = choice(role)
    if picked.connection == SAME_AS_PLANNER:
        picked = choice("planner")
    return connections.get(picked.connection) if picked.connection else None


def planner_factory(config: AppConfig) -> ChatFactory:
    """Builds the planner's chat model; ``thinking_enabled`` is accepted for the callers' sake."""
    del config
    return _chat_factory("planner")


def supervisor_factory(config: AppConfig) -> Callable[[], BaseChatModel]:
    del config
    build = _chat_factory("supervisor")
    return lambda: build(False)


def _chat_factory(role: str) -> ChatFactory:
    item = connection_for(role)
    if item is None:
        raise HTTPException(status_code=400, detail=f"The {role} has no model yet. Add a connection in Settings → Connections.")
    return lambda thinking=False: connections.chat_model(item)


def planner_model(config: AppConfig) -> str:
    """The planner's model name, for the run's context and the lessons."""
    del config
    item = connection_for("planner")
    return connections.model_name(item) if item else ""


def planner_sees_images(config: AppConfig) -> bool:
    """Whether the planner can be shown the blocked screen when it revises a plan."""
    del config
    item = connection_for("planner")
    return bool(item and item.kind == connections.VLM)


def supervisor_sees_images(config: AppConfig) -> bool:
    """The supervisor is a VLM connection, so it can always be sent screenshots."""
    del config
    item = connection_for("supervisor")
    return bool(item and item.kind == connections.VLM)


def vision_model(config: AppConfig) -> tuple[BaseChatModel | None, str]:
    """The computer-use model and its name; (None, "") when the role points at nothing."""
    del config
    item = connection_for("vision")
    return (connections.chat_model(item), connections.model_name(item)) if item else (None, "")


def current_embeddings(config: AppConfig) -> tuple[Any, str, ModelInfo] | None:
    """The embeddings model in use: the object, the id its vectors are stored under, and its info."""
    del config
    item = connection_for("embeddings")
    if item is None:
        return None
    return connections.embeddings_model(item), f"{item.id}:{connections.model_name(item)}", _info(item)


def _info(item: connections.Connection) -> ModelInfo:
    return ModelInfo(name=item.id, label=item.name, model=connections.model_name(item), where=connections.where(item), sees_images=item.kind == connections.VLM)


def role_info(role: str, config: AppConfig) -> ModelInfo:
    """What the UI and the check show for a role: its nickname, model and host."""
    del config
    item = connection_for(role)
    return _info(item) if item else ModelInfo(name=role, label="none", model=None, where="—")


# --- listing ------------------------------------------------------------------------


def overview(config: AppConfig) -> dict[str, Any]:
    """Everything the models and connections screens need."""
    del config
    return {
        "roles": {role: choice(role).model_dump() for role in ROLES},
        "role_kinds": {role: list(kinds) for role, kinds in ROLE_KINDS.items()},
        "kinds": [{"id": kind, "label": connections.KIND_LABELS[kind]} for kind in connections.KINDS],
        "connections": [connection_json(item) for item in connections.list_connections()],
        "interfaces": [
            {
                "path": item.path,
                "label": item.label,
                "kinds": list(item.kinds),
                "params": [{"name": p.name, "type": p.type, "required": p.required, "default": p.default, "hint": p.hint} for p in item.params],
            }
            for item in connections.INTERFACES
        ],
        "where": {role: role_info(role, None).model_dump() for role in ROLES},  # type: ignore[arg-type]
    }


def connection_json(item: connections.Connection) -> dict[str, Any]:
    return {"id": item.id, "name": item.name, "kind": item.kind, "interface": item.interface, "params": item.params, "secrets_set": list(item.secrets_set)}


# --- checking -------------------------------------------------------------------------


async def check_role(role: str, config: AppConfig) -> Check:
    """Ask this role's model one real question, so a wrong key or address shows up now."""
    info = role_info(role, config)
    item = connection_for(role)
    if item is None:
        detail = "Memory search is on keywords only." if role == "embeddings" else "This role points at no connection yet."
        return Check(role=role, name=info.name, model=info.model, where=info.where, ok=False, detail=detail)
    if role == "embeddings":
        return await _check_embeddings(config, info)

    async def call() -> str:
        reply = await connections.chat_model(item).ainvoke([HumanMessage(content="Reply with the single word OK.")])
        return f"answered: {message_text(reply).strip()[:40]!r}"

    return await _timed(role, info, call)


async def check_connections(config: AppConfig) -> list[Check]:
    """Every role in use, tested in parallel (the screen also checks them one at a time)."""
    roles = ["planner", "vision"]
    if get_state_value("vision_recovery", False) or get_state_value("verifier", "fara") == "supervisor":
        roles.append("supervisor")
    if choice("embeddings").connection:
        roles.append("embeddings")
    return list(await asyncio.gather(*(check_role(role, config) for role in roles)))


async def _check_embeddings(config: AppConfig, info: ModelInfo) -> Check:
    in_use = current_embeddings(config)
    if in_use is None:
        return Check(role="embeddings", name=info.name, model=info.model, where=info.where, ok=False, detail="Memory search is on keywords only.")

    async def call() -> str:
        (vector,) = await in_use[0].aembed_documents(["connection check"])
        return f"returned a {len(vector)}-dimensional vector"

    return await _timed("embeddings", info, call)


async def _timed(role: str, info: ModelInfo, call) -> Check:
    started = time.perf_counter()
    try:
        detail = await asyncio.wait_for(call(), CHECK_TIMEOUT)
        ok = True
    except TimeoutError:
        detail, ok = f"no answer within {CHECK_TIMEOUT:.0f} s", False
    except Exception as exc:
        detail, ok = f"{exc.__class__.__name__}: {str(exc).strip()[:300]}", False
    return Check(role=role, name=info.name, model=info.model, where=info.where, ok=ok, latency_ms=round((time.perf_counter() - started) * 1000), detail=detail)
