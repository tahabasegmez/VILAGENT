"""Experience-memory wiring for the gateway: the learner and the embeddings model of the selected preset."""

from __future__ import annotations

import logging

from vilagent.config.app_config import get_app_config
from vilagent.memory.embeddings import Embedder
from vilagent.memory.learn import Learner, model_distiller
from vilagent.memory.store import MemoryStore
from vilagent.server.models import current_embeddings, planner_factory
from vilagent.server.state import get_state_value

logger = logging.getLogger(__name__)


def memory_enabled() -> bool:
    return bool(get_state_value("memory_enabled", True))


def current_embedder() -> Embedder | None:
    """The embeddings model chosen now (the Memory setting); None for keyword-only search."""
    try:
        in_use = current_embeddings(get_app_config())
    except Exception:
        logger.warning("Could not build the embeddings model; memory falls back to keyword search", exc_info=True)
        return None
    return Embedder(in_use[0], in_use[1]) if in_use else None


def build_learner(store: MemoryStore) -> Learner:
    def lesson_model():
        return planner_factory(get_app_config())(False)

    return Learner(store, embedder=current_embedder, distill=model_distiller(lesson_model), enabled=memory_enabled)
