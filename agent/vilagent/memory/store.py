"""The memory database: ``<data dir>/.vilagent/memory.sqlite`` (SQLite, WAL, FTS5 for keyword search).

- ``episodes``: runs that completed (``verified`` says whether every step was checked): the masked task, the plan outline,
  the apps and domains they touched.
- ``lessons``: short reusable advice filed under an app, a domain or "general"; learned
  automatically from trouble (``source='auto'``) or written by the operator (``'operator'``).
- ``memory_usage``: which entries a run was given (Phase 9).

Schema changes are appended to ``MIGRATIONS``; ``PRAGMA user_version`` records how many ran.
"""

from __future__ import annotations

import difflib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from vilagent.memory.embeddings import MIN_SIMILARITY, similarity

SAME_LESSON = 0.9  # similarity at which a new lesson counts as one already known

MIGRATIONS = [
    """
    CREATE TABLE episodes (
        id TEXT PRIMARY KEY, run_id TEXT, created_at TEXT, task_text TEXT NOT NULL, approach TEXT,
        environments TEXT, apps TEXT, domains TEXT, plan_outline TEXT, outcome TEXT, verified INTEGER,
        actions INTEGER, duration_s REAL, rating TEXT, embedding BLOB, embedding_model TEXT, uses INTEGER DEFAULT 0
    );
    CREATE TABLE lessons (
        id TEXT PRIMARY KEY, key_type TEXT NOT NULL, key TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL,
        from_run_id TEXT, created_at TEXT, updated_at TEXT, hits INTEGER DEFAULT 1, disabled INTEGER DEFAULT 0,
        embedding BLOB, embedding_model TEXT, uses INTEGER DEFAULT 0
    );
    CREATE INDEX lessons_by_key ON lessons(key_type, key);
    CREATE TABLE memory_usage (run_id TEXT NOT NULL, entry_type TEXT NOT NULL, entry_id TEXT NOT NULL, rank INTEGER);
    CREATE VIRTUAL TABLE episodes_fts USING fts5(task_text, apps, domains, content='episodes');
    CREATE VIRTUAL TABLE lessons_fts USING fts5(key, text, content='lessons');
    CREATE TRIGGER episodes_ai AFTER INSERT ON episodes BEGIN
        INSERT INTO episodes_fts(rowid, task_text, apps, domains) VALUES (new.rowid, new.task_text, new.apps, new.domains);
    END;
    CREATE TRIGGER episodes_ad AFTER DELETE ON episodes BEGIN
        INSERT INTO episodes_fts(episodes_fts, rowid, task_text, apps, domains) VALUES ('delete', old.rowid, old.task_text, old.apps, old.domains);
    END;
    CREATE TRIGGER episodes_au AFTER UPDATE OF task_text, apps, domains ON episodes BEGIN
        INSERT INTO episodes_fts(episodes_fts, rowid, task_text, apps, domains) VALUES ('delete', old.rowid, old.task_text, old.apps, old.domains);
        INSERT INTO episodes_fts(rowid, task_text, apps, domains) VALUES (new.rowid, new.task_text, new.apps, new.domains);
    END;
    CREATE TRIGGER lessons_ai AFTER INSERT ON lessons BEGIN
        INSERT INTO lessons_fts(rowid, key, text) VALUES (new.rowid, new.key, new.text);
    END;
    CREATE TRIGGER lessons_ad AFTER DELETE ON lessons BEGIN
        INSERT INTO lessons_fts(lessons_fts, rowid, key, text) VALUES ('delete', old.rowid, old.key, old.text);
    END;
    CREATE TRIGGER lessons_au AFTER UPDATE OF key, text ON lessons BEGIN
        INSERT INTO lessons_fts(lessons_fts, rowid, key, text) VALUES ('delete', old.rowid, old.key, old.text);
        INSERT INTO lessons_fts(rowid, key, text) VALUES (new.rowid, new.key, new.text);
    END;
    """,
]

_EMBEDDED_TEXT = {"episodes": "task_text", "lessons": "text"}
_EDITABLE = {"episodes": {"rating"}, "lessons": {"text", "key_type", "key", "disabled"}}
# Rows recall may return: the operator can mark an episode bad or switch a lesson off.
_USABLE = {"episodes": "(rating IS NULL OR rating != 'bad')", "lessons": "disabled = 0"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Episode:
    run_id: str
    task_text: str
    approach: str
    plan_outline: list[dict[str, Any]]
    apps: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    environments: list[str] = field(default_factory=list)
    actions: int = 0
    duration_s: float | None = None
    #: False when a step finished without a check, so recall can prefer the checked runs.
    verified: bool = True


class MemoryStore:
    def __init__(self, db: aiosqlite.Connection):
        self._db = db

    @classmethod
    async def open(cls, path: str | Path) -> MemoryStore:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(str(path))
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        (version,) = await (await db.execute("PRAGMA user_version")).fetchone()
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            await db.executescript(script)
            await db.execute(f"PRAGMA user_version = {number}")
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    # --- writing ---------------------------------------------------------------

    async def add_episode(self, episode: Episode, *, embedding: bytes | None = None, model: str | None = None) -> str:
        episode_id = uuid.uuid4().hex
        await self._db.execute(
            "INSERT INTO episodes (id, run_id, created_at, task_text, approach, environments, apps, domains, plan_outline, outcome, verified,"
            " actions, duration_s, embedding, embedding_model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'success', ?, ?, ?, ?, ?)",
            (
                episode_id, episode.run_id, _now(), episode.task_text, episode.approach, json.dumps(episode.environments), json.dumps(episode.apps),
                json.dumps(episode.domains), json.dumps(episode.plan_outline), int(episode.verified), episode.actions, episode.duration_s,
                embedding, model if embedding else None,
            ),
        )
        await self._db.commit()
        return episode_id

    async def add_lesson(
        self, key_type: str, key: str, text: str, *, source: str, from_run_id: str | None = None, embedding: bytes | None = None, model: str | None = None
    ) -> tuple[str, str]:
        """Insert, or merge into a lesson already known under the same key: ("inserted" | "merged", id)."""
        for known in await self.lessons(key_type=key_type, key=key):
            if known["source"] == source and _same(text, embedding, model, known):
                longer = text if len(text) > len(known["text"]) else known["text"]
                await self._db.execute("UPDATE lessons SET hits = hits + 1, text = ?, updated_at = ? WHERE id = ?", (longer, _now(), known["id"]))
                await self._db.commit()
                return "merged", known["id"]
        lesson_id = uuid.uuid4().hex
        await self._db.execute(
            "INSERT INTO lessons (id, key_type, key, text, source, from_run_id, created_at, updated_at, embedding, embedding_model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lesson_id, key_type, key, text, source, from_run_id, _now(), _now(), embedding, model if embedding else None),
        )
        await self._db.commit()
        return "inserted", lesson_id

    async def update(self, table: str, entry_id: str, **fields: Any) -> bool:
        """Operator edits: rate an episode (``rating``), or edit or switch off a lesson."""
        unknown = set(fields) - _EDITABLE[table]
        if unknown or not fields:
            raise ValueError(f"Cannot edit {sorted(unknown) or 'nothing'} on {table}")
        if "text" in fields:  # a new wording needs a new vector
            fields |= {"embedding": None, "embedding_model": None}
        if table == "lessons":
            fields["updated_at"] = _now()
        assignments = ", ".join(f"{column} = ?" for column in fields)
        cursor = await self._db.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", [*fields.values(), entry_id])
        await self._db.commit()
        return cursor.rowcount > 0

    async def delete(self, table: str, entry_id: str) -> bool:
        if table not in _EDITABLE:
            raise ValueError(table)
        cursor = await self._db.execute(f"DELETE FROM {table} WHERE id = ?", (entry_id,))
        await self._db.execute("DELETE FROM memory_usage WHERE entry_type = ? AND entry_id = ?", (table, entry_id))
        await self._db.commit()
        return cursor.rowcount > 0

    async def clear(self) -> None:
        """Forget everything (the operator's reset)."""
        for table in ("memory_usage", "episodes", "lessons"):
            await self._db.execute(f"DELETE FROM {table}")
        await self._db.commit()

    async def rate_run(self, run_id: str, rating: str | None) -> bool:
        """Rate the episode a run became (False when the run was not kept as an example)."""
        cursor = await self._db.execute("UPDATE episodes SET rating = ? WHERE run_id = ?", (rating, run_id))
        await self._db.commit()
        return cursor.rowcount > 0

    async def used_by(self, run_id: str) -> dict[str, list[dict[str, Any]]]:
        """The entries a run was given (``memory_usage``), in rank order, as they are now."""
        cursor = await self._db.execute("SELECT entry_type, entry_id FROM memory_usage WHERE run_id = ? ORDER BY rank", (run_id,))
        usage = await cursor.fetchall()
        return {table: await self.get(table, [row[1] for row in usage if row[0] == table], usable=False) for table in ("episodes", "lessons")}

    async def rows_to_embed(self, model: str, limit: int) -> list[tuple[str, str, str]]:
        """(table, id, text) of rows with no vector from ``model``."""
        rows = []
        for table, column in _EMBEDDED_TEXT.items():
            cursor = await self._db.execute(f"SELECT id, {column} FROM {table} WHERE embedding_model IS NOT ? LIMIT ?", (model, limit - len(rows)))
            rows += [(table, row[0], row[1]) for row in await cursor.fetchall()]
            if len(rows) >= limit:
                break
        return rows

    async def rows_for_embedding(self, table: str, ids: list[str] | None = None) -> list[tuple[str, str]]:
        """(id, text) of the rows to embed: the ones named, else every row of the table."""
        if table not in _EMBEDDED_TEXT:
            raise ValueError(table)
        column = _EMBEDDED_TEXT[table]
        if ids is None:
            cursor = await self._db.execute(f"SELECT id, {column} FROM {table}")
        else:
            cursor = await self._db.execute(f"SELECT id, {column} FROM {table} WHERE id IN ({','.join('?' * len(ids))})", ids)
        return [(row[0], row[1]) for row in await cursor.fetchall()]

    async def set_embedding(self, table: str, row_id: str, embedding: bytes, model: str) -> None:
        if table not in _EMBEDDED_TEXT:
            raise ValueError(table)
        await self._db.execute(f"UPDATE {table} SET embedding = ?, embedding_model = ? WHERE id = ?", (embedding, model, row_id))
        await self._db.commit()

    # --- reading ---------------------------------------------------------------

    async def episodes(self) -> list[dict[str, Any]]:
        return [_row(row) for row in await (await self._db.execute("SELECT * FROM episodes ORDER BY created_at DESC")).fetchall()]

    async def lessons(self, *, key_type: str | None = None, key: str | None = None) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM lessons", []
        if key_type is not None:
            query, params = query + " WHERE key_type = ? AND key = ?", [key_type, key]
        return [_row(row) for row in await (await self._db.execute(query + " ORDER BY hits DESC, updated_at DESC", params)).fetchall()]

    async def nearest(self, table: str, embedding: bytes, model: str, limit: int = 20) -> list[str]:
        """Ids of usable rows (episodes not rated bad, lessons enabled) close to ``embedding``, best first.

        Only rows that are actually close come back: without a floor the top of the list is filled
        with whatever is least unrelated, and an old task ends up shown as an example for a new one.
        """
        cursor = await self._db.execute(f"SELECT id, embedding FROM {table} WHERE embedding_model = ? AND {_USABLE[table]}", (model,))
        scored = [(score, row[0]) for row in await cursor.fetchall() if (score := similarity(embedding, row[1])) >= MIN_SIMILARITY]
        return [row_id for _, row_id in sorted(scored, reverse=True)[:limit]]

    async def get(self, table: str, ids: list[str], *, usable: bool = True) -> list[dict[str, Any]]:
        """Rows by id, in the order of ``ids``; ``usable`` leaves out bad-rated episodes and disabled lessons."""
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        condition = _USABLE[table] if usable else "1"
        cursor = await self._db.execute(f"SELECT * FROM {table} WHERE id IN ({marks}) AND {condition}", ids)
        rows = {row["id"]: _row(row) for row in await cursor.fetchall()}
        return [rows[row_id] for row_id in ids if row_id in rows]

    async def lessons_for_keys(self, apps: list[str], domains: list[str]) -> list[dict[str, Any]]:
        """Enabled lessons filed under these apps or domains: operator notes first, then the most confirmed."""
        pairs = [("app", key) for key in apps] + [("domain", key) for key in domains]
        if not pairs:
            return []
        where = " OR ".join("(key_type = ? AND key = ?)" for _ in pairs)
        cursor = await self._db.execute(
            f"SELECT * FROM lessons WHERE disabled = 0 AND ({where}) ORDER BY source = 'operator' DESC, hits DESC, updated_at DESC",
            [value for pair in pairs for value in pair],
        )
        return [_row(row) for row in await cursor.fetchall()]

    async def lesson_keys(self, key_type: str) -> list[str]:
        cursor = await self._db.execute("SELECT DISTINCT key FROM lessons WHERE key_type = ? AND disabled = 0", (key_type,))
        return [row[0] for row in await cursor.fetchall()]

    async def record_usage(self, run_id: str, entries: list[tuple[str, str]]) -> None:
        """Which entries (``(table, id)``) a run was given, in rank order; bumps their ``uses``."""
        for rank, (table, entry_id) in enumerate(entries):
            await self._db.execute("INSERT INTO memory_usage (run_id, entry_type, entry_id, rank) VALUES (?, ?, ?, ?)", (run_id, table, entry_id, rank))
            await self._db.execute(f"UPDATE {table} SET uses = uses + 1 WHERE id = ?", (entry_id,))
        await self._db.commit()

    async def embedded_ids(self, table: str, model: str) -> set[str]:
        """Ids this model has a vector for, so a keyword hit it judged unrelated can be dropped."""
        if table not in _EMBEDDED_TEXT:
            raise ValueError(table)
        cursor = await self._db.execute(f"SELECT id FROM {table} WHERE embedding_model = ?", (model,))
        return {row[0] for row in await cursor.fetchall()}

    async def search_text(self, table: str, query: str, limit: int = 20) -> list[str]:
        """Ids of rows matching ``query`` by keyword (FTS5 bm25), best first."""
        if table not in _EMBEDDED_TEXT:
            raise ValueError(table)
        terms = " OR ".join(f'"{word}"' for word in {w.lower() for w in _words(query)})
        if not terms:
            return []
        cursor = await self._db.execute(
            f"SELECT t.id FROM {table}_fts f JOIN {table} t ON t.rowid = f.rowid WHERE {table}_fts MATCH ? ORDER BY bm25({table}_fts) LIMIT ?", (terms, limit)
        )
        return [row[0] for row in await cursor.fetchall()]


def _same(text: str, embedding: bytes | None, model: str | None, known: dict[str, Any]) -> bool:
    if embedding is not None and known.get("embedding") is not None and model and known.get("embedding_model") == model:
        return similarity(embedding, known["embedding"]) >= SAME_LESSON
    return difflib.SequenceMatcher(None, text.lower(), known["text"].lower()).ratio() >= SAME_LESSON


def _words(text: str) -> list[str]:
    return [word for word in "".join(ch if ch.isalnum() else " " for ch in text).split() if len(word) > 1]


def public(row: dict[str, Any]) -> dict[str, Any]:
    """A row as the UI sees it: no vector bytes."""
    return {key: value for key, value in row.items() if key != "embedding"}


def _row(row: aiosqlite.Row) -> dict[str, Any]:
    data = dict(row)
    if "embedding" in data:
        data["embedded"] = bool(data.get("embedding"))
    for column in ("environments", "apps", "domains", "plan_outline"):
        if isinstance(data.get(column), str):
            data[column] = json.loads(data[column])
    return data
