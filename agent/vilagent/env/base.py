"""What an agent can do with a place it operates in: look at it and act on it."""

from __future__ import annotations

from abc import ABC, abstractmethod

from vilagent.actions import Action, ActionOutcome
from vilagent.control import Control


class Environment(ABC):
    """A screen the vision model sees (``screenshot``) and drives (``act``)."""

    name: str

    def __init__(self, control: Control):
        self.control = control

    @abstractmethod
    async def screenshot(self) -> bytes:
        """PNG bytes of what the model should look at; pixel = action coordinate."""

    async def act(self, action: Action) -> ActionOutcome:
        return await self.control.perform(self.name, action, self._execute)

    @abstractmethod
    async def _execute(self, action: Action) -> ActionOutcome:
        """Carry out one action. Only called through :meth:`act`."""

    def context_hint(self) -> str | None:
        """Extra line for the model prompt (e.g. the current page URL)."""
        return None
