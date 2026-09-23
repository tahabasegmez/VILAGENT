"""Embedding vectors for memory rows: L2-normalized float32 blobs, compared by dot product.

Pure Python on purpose: the frozen app excludes numpy, and memory holds a few thousand rows
at most. A vector is only comparable with vectors of the same model id; rows embedded by an
older model are re-embedded in the background (``reembed``).
"""

from __future__ import annotations

import logging
import math
from array import array
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vilagent.memory.store import MemoryStore

logger = logging.getLogger(__name__)

BATCH = 32
# How close a vector must be to count as relevant at all. Measured on a real store: the same
# task again scores 0.79-1.00, an unrelated one 0.17-0.27, so anything under this is noise and
# recall returns fewer rows rather than the best of a bad lot.
MIN_SIMILARITY = 0.45


def pack(vector: Sequence[float]) -> bytes:
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return array("f", (value / norm for value in vector)).tobytes()


def unpack(blob: bytes) -> array:
    vector = array("f")
    vector.frombytes(blob)
    return vector


def similarity(a: bytes, b: bytes) -> float:
    """Cosine similarity of two packed (already normalized) vectors."""
    return sum(x * y for x, y in zip(unpack(a), unpack(b), strict=False))


class Embedder:
    """One embeddings model (a LangChain ``Embeddings``) and the id its vectors are stored under."""

    def __init__(self, model: Any, model_id: str):
        self._model = model
        self.model_id = model_id

    async def embed(self, texts: list[str]) -> list[bytes] | None:
        """Packed vectors, or None when the provider fails (rows are then stored without one)."""
        try:
            return [pack(vector) for vector in await self._model.aembed_documents(texts)]
        except Exception:
            logger.warning("Embedding %d text(s) with %s failed; storing without vectors", len(texts), self.model_id, exc_info=True)
            return None


async def embed_entries(store: MemoryStore, embedder: Embedder, table: str, ids: list[str] | None = None) -> int:
    """Embed these rows again with the current model, whatever they carried before."""
    rows = await store.rows_for_embedding(table, ids)
    done = 0
    for start in range(0, len(rows), BATCH):
        batch = rows[start : start + BATCH]
        vectors = await embedder.embed([text for _, text in batch])
        if vectors is None:
            break
        for (row_id, _), vector in zip(batch, vectors, strict=True):
            await store.set_embedding(table, row_id, vector, embedder.model_id)
        done += len(batch)
    return done


async def reembed(store: MemoryStore, embedder: Embedder | None) -> int:
    """Give every row without a vector of the current model one; returns how many were embedded."""
    if embedder is None:
        return 0
    done = 0
    while rows := await store.rows_to_embed(embedder.model_id, BATCH):
        vectors = await embedder.embed([text for _, _, text in rows])
        if vectors is None:
            break
        for (table, row_id, _), vector in zip(rows, vectors, strict=True):
            await store.set_embedding(table, row_id, vector, embedder.model_id)
        done += len(rows)
    return done
