"""Every model the app can use: a LangChain connection the operator added, stored encrypted.

A **connection** is one model endpoint: a nickname, what type of model it is, the LangChain class
that talks to it, and that class's own constructor arguments. The arguments are stored and shown
under their real names — ``base_url``, ``api_key``, ``max_tokens`` — so what the operator fills in
is exactly what LangChain receives. Nothing here speaks HTTP itself: an API, an ngrok tunnel and a
local Ollama are all just a class with a different ``base_url``.

Four types, because the roles need different things:

- ``llm``          a text model (the planner);
- ``vlm``          a chat model that can be shown screenshots (the supervisor, the step check);
- ``computer_use`` the model that looks at the screen and acts on it (FARA);
- ``embedding``    memory search by meaning.

Keys are protected with Windows DPAPI, so the ciphertext only opens for the same Windows user on
the same machine. A key is write-only as far as the UI is concerned: it is sent in, and after that
the UI is only told that one is set.
"""

from __future__ import annotations

import importlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from vilagent.server.config import get_gateway_config

#: What a connection serves, and so which roles may point at it.
LLM, VLM, COMPUTER_USE, EMBEDDING = "llm", "vlm", "computer_use", "embedding"
KINDS = (LLM, VLM, COMPUTER_USE, EMBEDDING)
KIND_LABELS = {LLM: "LLM (text)", VLM: "VLM (text + images)", COMPUTER_USE: "Computer use (acts on the screen)", EMBEDDING: "Embedding"}
#: A parameter's type, so the UI can render it and we can hand LangChain the right Python value.
TEXT, SECRET, NUMBER, INTEGER, BOOLEAN = "text", "secret", "number", "integer", "boolean"
#: Tied to this app, so a blob from elsewhere cannot be decrypted through us by accident.
_ENTROPY = b"VILAGENT api connections v1"


@dataclass(frozen=True)
class Param:
    """One constructor argument, under the name LangChain itself uses."""

    name: str
    type: str = TEXT
    required: bool = False
    default: str = ""
    hint: str = ""


@dataclass(frozen=True)
class Interface:
    """A LangChain class, the types of connection it can serve, and its arguments."""

    path: str
    label: str
    kinds: tuple[str, ...]
    params: tuple[Param, ...]


# The defaults the app has always used (they were the config.yaml entries' settings).
_TIMEOUT = Param("timeout", NUMBER, default="60", hint="seconds")
_RETRIES = Param("max_retries", INTEGER, default="1")
_TEMPERATURE = Param("temperature", NUMBER, default="0.0")
_MAX_TOKENS = Param("max_tokens", INTEGER, default="2048", hint="reply cap")

#: Every interface the operator can pick. All of them are LangChain classes.
INTERFACES: tuple[Interface, ...] = (
    Interface(
        "langchain_openai:ChatOpenAI",
        "OpenAI-compatible",
        (LLM, VLM, COMPUTER_USE),
        (
            Param("model", TEXT, required=True, hint="gpt-4o-mini, microsoft/Fara-7B, …"),
            Param("base_url", TEXT, hint="https://…/v1"),
            Param("api_key", SECRET, default="not-needed", hint="if the endpoint needs one"),
            _TEMPERATURE,
            _MAX_TOKENS,
            _TIMEOUT,
            _RETRIES,
        ),
    ),
    Interface(
        "langchain_ollama:ChatOllama",
        "Ollama",
        (LLM, VLM, COMPUTER_USE),
        (
            Param("model", TEXT, required=True, hint="llama3.2-vision, …"),
            Param("base_url", TEXT, default="http://localhost:11434"),
            _TEMPERATURE,
            Param("num_predict", INTEGER, default="2048", hint="reply cap"),
        ),
    ),
    Interface(
        "langchain_google_genai:ChatGoogleGenerativeAI",
        "Google Gemini",
        (LLM, VLM),
        (
            Param("model", TEXT, required=True, hint="gemini-2.5-flash, …"),
            Param("google_api_key", SECRET, required=True),
            _TEMPERATURE,
            Param("max_output_tokens", INTEGER, default="2048"),
            _TIMEOUT,
            _RETRIES,
        ),
    ),
    Interface(
        "langchain_anthropic:ChatAnthropic",
        "Anthropic Claude",
        (LLM, VLM),
        (
            Param("model", TEXT, required=True, hint="claude-sonnet-4-5, …"),
            Param("api_key", SECRET, required=True),
            Param("base_url", TEXT),
            _TEMPERATURE,
            _MAX_TOKENS,
            _TIMEOUT,
            _RETRIES,
        ),
    ),
    Interface(
        "langchain_openai:OpenAIEmbeddings",
        "OpenAI-compatible",
        (EMBEDDING,),
        (
            Param("model", TEXT, required=True, hint="text-embedding-3-small, …"),
            Param("base_url", TEXT, hint="https://…/v1"),
            Param("api_key", SECRET, default="not-needed"),
            _TIMEOUT,
            _RETRIES,
            Param("check_embedding_ctx_length", BOOLEAN, default="false", hint="false unless it is OpenAI itself"),
        ),
    ),
    Interface(
        "langchain_ollama:OllamaEmbeddings",
        "Ollama",
        (EMBEDDING,),
        (Param("model", TEXT, required=True, hint="nomic-embed-text, …"), Param("base_url", TEXT, default="http://localhost:11434")),
    ),
    Interface(
        "langchain_google_genai:GoogleGenerativeAIEmbeddings",
        "Google Gemini",
        (EMBEDDING,),
        (Param("model", TEXT, required=True, hint="models/text-embedding-004"), Param("google_api_key", SECRET, required=True)),
    ),
    Interface(
        "langchain_huggingface:HuggingFaceEndpointEmbeddings",
        "Hugging Face",
        (EMBEDDING,),
        (
            Param("model", TEXT, required=True, hint="sentence-transformers/all-MiniLM-L6-v2, …"),
            Param("huggingfacehub_api_token", SECRET, required=True, hint="a read token"),
            # The router serves embeddings as feature extraction, not on its OpenAI-compatible path.
            Param("task", TEXT, default="feature-extraction"),
            Param("provider", TEXT, hint="auto, hf-inference, …"),
        ),
    ),
)


@dataclass(frozen=True)
class Connection:
    id: str
    name: str
    kind: str
    interface: str
    #: The interface's arguments as the operator gave them; secrets are not in here.
    params: dict[str, str]
    #: Which secret arguments have a value stored.
    secrets_set: tuple[str, ...]


def db_path():
    return get_gateway_config().data_dir / "connections.db"


def interface(path: str) -> Interface | None:
    return next((item for item in INTERFACES if item.path == path), None)


def interfaces_for(kind: str) -> list[Interface]:
    return [item for item in INTERFACES if kind in item.kinds]


def model_name(item: Connection) -> str:
    return item.params.get("model", "")


def where(item: Connection) -> str:
    """The host it talks to, for the UI and the connection check."""
    base_url = item.params.get("base_url", "")
    if base_url:
        return urlparse(base_url).hostname or base_url
    return item.interface.split(":")[0].replace("langchain_", "")


# --- the store ------------------------------------------------------------------------


def protect(secret: str) -> bytes:
    """Encrypt for this Windows user (empty stays empty)."""
    if not secret:
        return b""
    import win32crypt

    return win32crypt.CryptProtectData(secret.encode("utf-8"), "VILAGENT", _ENTROPY, None, None, 0)


def unprotect(blob: bytes) -> str:
    """Read back; a blob from another user or machine simply does not decrypt."""
    if not blob:
        return ""
    import win32crypt

    return win32crypt.CryptUnprotectData(bytes(blob), _ENTROPY, None, None, 0)[1].decode("utf-8")


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS connections ("
    "id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, interface TEXT NOT NULL, "
    "params TEXT NOT NULL DEFAULT '{}', secrets BLOB NOT NULL, created_at TEXT NOT NULL)"
)
#: What an earlier version called the types.
_OLD_KINDS = {"chat": LLM, "openai": LLM, "google": LLM, "vision": COMPUTER_USE, "embeddings": EMBEDDING}


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute(_SCHEMA)
    _migrate(db)
    return db


def _migrate(db: sqlite3.Connection) -> None:
    """Rebuild a table from an earlier version into the current shape, keeping what it held."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(connections)")}
    if {"interface", "params", "secrets"} <= columns and "secret" not in columns:
        return
    rows = [_converted(row, columns) for row in db.execute("SELECT * FROM connections").fetchall()]
    db.execute("DROP TABLE connections")
    db.execute(_SCHEMA)
    db.executemany("INSERT INTO connections (id, name, kind, interface, params, secrets, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def _converted(row: sqlite3.Row, columns: set[str]) -> tuple:
    """An older row in the current columns: its model and address become the interface's arguments."""
    kind = _OLD_KINDS.get(row["kind"], row["kind"])
    google = row["kind"] == "google" or ("interface" in columns and str(row["interface"]).startswith("langchain_google_genai:"))
    if kind == EMBEDDING:
        interface_path = "langchain_google_genai:GoogleGenerativeAIEmbeddings" if google else "langchain_openai:OpenAIEmbeddings"
    else:
        interface_path = "langchain_google_genai:ChatGoogleGenerativeAI" if google else "langchain_openai:ChatOpenAI"
    params = {name: row[name] for name in ("model", "base_url") if name in columns and row[name]}
    # The single key an older row kept becomes the interface's own key argument.
    key = unprotect(row["secret"]) if "secret" in columns and row["secret"] else ""
    secrets = {("google_api_key" if google else "api_key"): key} if key else {}
    return (row["id"], row["name"], kind, interface_path, json.dumps(params), protect(json.dumps(secrets)) if secrets else b"", row["created_at"])


def _row(row: sqlite3.Row) -> Connection:
    params = json.loads(row["params"])
    return Connection(id=row["id"], name=row["name"], kind=row["kind"], interface=row["interface"], params=params, secrets_set=tuple(sorted(_secrets(row["secrets"]))))


def _secrets(blob: bytes) -> dict[str, str]:
    try:
        return json.loads(unprotect(blob) or "{}")
    except Exception:  # another Windows user, another machine, or a corrupted blob
        return {}


def list_connections(kind: str = "") -> list[Connection]:
    """Every connection (or every one of a type), oldest first, without the keys."""
    with _connect() as db:
        rows = db.execute("SELECT * FROM connections ORDER BY created_at, id").fetchall()
    return [_row(row) for row in rows if not kind or row["kind"] == kind]


def get(connection_id: str) -> Connection | None:
    with _connect() as db:
        row = db.execute("SELECT * FROM connections WHERE id = ?", (connection_id,)).fetchone()
    return _row(row) if row else None


def secrets_of(connection_id: str) -> dict[str, str]:
    """The decrypted secret arguments ({} when there are none or they cannot be read here)."""
    with _connect() as db:
        row = db.execute("SELECT secrets FROM connections WHERE id = ?", (connection_id,)).fetchone()
    return _secrets(row["secrets"]) if row else {}


def save(name: str, kind: str, interface_path: str, values: dict[str, str], connection_id: str = "") -> Connection:
    """Add a connection, or update one. A secret left out of ``values`` keeps the stored one."""
    name = name.strip()
    if not name:
        raise ValueError("A connection needs a nickname.")
    if kind not in KINDS:
        raise ValueError(f"Unknown connection type {kind!r}.")
    known = interface(interface_path)
    if known is None or kind not in known.kinds:
        raise ValueError(f"{interface_path!r} cannot serve a {KIND_LABELS.get(kind, kind)} connection.")
    params, secrets = _split(known, values)
    existing = get(connection_id) if connection_id else None
    if connection_id and existing is None:
        raise ValueError(f"No connection named {connection_id!r}.")
    kept = {name_: value for name_, value in secrets_of(connection_id).items() if name_ not in secrets} if existing else {}
    secrets = kept | secrets
    for param in known.params:
        if param.required and not (secrets.get(param.name) if param.type == SECRET else params.get(param.name)):
            raise ValueError(f"{param.name} is required for {known.path}.")
    blob = protect(json.dumps(secrets)) if secrets else b""
    with _connect() as db:
        if existing is not None:
            db.execute("UPDATE connections SET name = ?, kind = ?, interface = ?, params = ?, secrets = ? WHERE id = ?", (name, kind, interface_path, json.dumps(params), blob, existing.id))
            return Connection(id=existing.id, name=name, kind=kind, interface=interface_path, params=params, secrets_set=tuple(sorted(secrets)))
        new_id = _free_id(db, name)
        db.execute(
            "INSERT INTO connections (id, name, kind, interface, params, secrets, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_id, name, kind, interface_path, json.dumps(params), blob, datetime.now(UTC).isoformat()),
        )
        return Connection(id=new_id, name=name, kind=kind, interface=interface_path, params=params, secrets_set=tuple(sorted(secrets)))


def delete(connection_id: str) -> bool:
    with _connect() as db:
        return db.execute("DELETE FROM connections WHERE id = ?", (connection_id,)).rowcount > 0


def _split(known: Interface, values: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Keep only the interface's own arguments, and put the secrets aside to be encrypted."""
    params: dict[str, str] = {}
    secrets: dict[str, str] = {}
    for param in known.params:
        value = str(values.get(param.name, "")).strip()
        if not value:
            continue
        (secrets if param.type == SECRET else params)[param.name] = value
    return params, secrets


def _free_id(db: sqlite3.Connection, name: str) -> str:
    """A readable id from the nickname, with a number when that one is taken."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "connection"
    taken = {row["id"] for row in db.execute("SELECT id FROM connections")}
    candidate, suffix = base, 2
    while candidate in taken:
        candidate, suffix = f"{base}-{suffix}", suffix + 1
    return candidate


# --- building the client ---------------------------------------------------------------


def resolve_class(path: str) -> type:
    """Import ``package.module:Class``."""
    module_name, _, class_name = path.replace(":", ".").rpartition(".")
    try:
        return getattr(importlib.import_module(module_name), class_name)
    except (ImportError, AttributeError) as exc:
        raise ImportError(f"Could not import {path!r}: {exc}") from exc


def build(item: Connection) -> Any:
    """The LangChain client for this connection, with its arguments under their own names."""
    known = interface(item.interface)
    if known is None:
        raise ValueError(f"{item.interface!r} is no longer offered; edit the connection.")
    values = dict(item.params) | secrets_of(item.id)
    # An argument the operator left empty falls back to the interface's own default.
    kwargs = {param.name: _coerce(param, values.get(param.name) or param.default) for param in known.params if values.get(param.name) or param.default}
    return resolve_class(item.interface)(**kwargs)


def chat_model(item: Connection) -> BaseChatModel:
    built = build(item)
    if not isinstance(built, BaseChatModel):
        raise TypeError(f"{item.interface!r} is not a LangChain chat model class")
    return built


def embeddings_model(item: Connection) -> Embeddings:
    built = build(item)
    if not isinstance(built, Embeddings):
        raise TypeError(f"{item.interface!r} is not a LangChain embeddings class")
    return built


def _coerce(param: Param, value: str) -> Any:
    if param.type == NUMBER:
        return float(value)
    if param.type == INTEGER:
        return int(float(value))
    if param.type == BOOLEAN:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    return value
