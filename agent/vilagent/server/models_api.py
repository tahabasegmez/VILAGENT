"""The connections the operator keeps, which one plays each role, and the checks."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from vilagent import connections
from vilagent.config.app_config import AppConfig
from vilagent.server import models
from vilagent.server.deps import get_config, require_internal_request
from vilagent.server.state import set_state_value

router = APIRouter(prefix="/api/computer-use", tags=["models"], dependencies=[Depends(require_internal_request)])

ROLE_PATTERN = "^(planner|supervisor|vision|embeddings)$"


class RoleChoice(BaseModel):
    """Which connection a role uses. ``connection`` "" turns the role off (memory search only)."""

    connection: str = Field(default="", max_length=64)
    model_config = ConfigDict(extra="forbid")


class ConnectionBody(BaseModel):
    """One model endpoint: a nickname, its type, its LangChain class and that class's arguments."""

    name: str = Field(min_length=1, max_length=64)
    kind: str = Field(pattern=f"^({'|'.join(connections.KINDS)})$")
    interface: str = Field(min_length=1, max_length=200)
    #: The interface's own arguments, by their LangChain names. A secret left out keeps the stored one.
    params: dict[str, str] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")


@router.get("/models", summary="Each role's connection, every saved connection, and the interfaces")
async def get_models(config: AppConfig = Depends(get_config)) -> dict:
    return models.overview(config)


@router.post("/models/{role}", summary="Point a role at a connection")
async def select_role(body: RoleChoice, role: str = PathParam(pattern=ROLE_PATTERN), config: AppConfig = Depends(get_config)) -> dict:
    picked = body.connection.strip()
    if picked == models.SAME_AS_PLANNER:
        _require(role == "supervisor", "Only the supervisor can follow the planner.")
    elif picked:
        item = connections.get(picked)
        _require(item is not None, f"No connection named {picked!r}.")
        kinds = models.ROLE_KINDS[role]
        _require(item is not None and item.kind in kinds, f"The {role} needs a {' or '.join(kinds)} connection.")
    else:
        _require(role == "embeddings", "This role needs a connection.")
    set_state_value(f"{role}_connection", picked)
    return models.overview(config)


@router.get("/connections", summary="The connections the operator has saved")
async def get_connections() -> list[dict]:
    return [models.connection_json(item) for item in connections.list_connections()]


@router.post("/connections", summary="Add or update a connection")
async def save_connection(body: ConnectionBody, connection_id: str = "", config: AppConfig = Depends(get_config)) -> dict:
    try:
        connections.save(body.name, body.kind, body.interface, body.params, connection_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # DPAPI is the only way we store a key; say so rather than half-saving
        raise HTTPException(status_code=500, detail=f"The connection could not be saved: {exc}") from exc
    return models.overview(config)


@router.delete("/connections/{connection_id}", summary="Forget a connection and its keys")
async def delete_connection(connection_id: str, config: AppConfig = Depends(get_config)) -> dict:
    _require(connections.delete(connection_id), f"No connection named {connection_id!r}.")
    return models.overview(config)


@router.post("/models/{role}/check", response_model=models.Check, summary="Ask this role's model one real question")
async def check_role(role: str = PathParam(pattern=ROLE_PATTERN), config: AppConfig = Depends(get_config)) -> models.Check:
    return await models.check_role(role, config)


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise HTTPException(status_code=422, detail=detail)
