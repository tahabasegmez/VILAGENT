"""Test doubles for environments and the vision model."""

from __future__ import annotations

import io
from collections.abc import Iterable

from vilagent.actions import Action, ActionKind, ActionOutcome, Target
from vilagent.config.computer_use_config import ComputerUseConfig
from vilagent.control import Control
from vilagent.env.base import Environment
from vilagent.vision.fara import Verification


def png(color=(255, 255, 255), size=(40, 30)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def click(x: int = 10, y: int = 20, thought: str | None = None) -> Action:
    return Action(kind=ActionKind.click, target=Target.at(x, y), thought=thought)


def finish(status: str = "success") -> Action:
    return Action(kind=ActionKind.finish, args={"status": status})


class FakeEnv(Environment):
    """Records actions; each action changes the screenshot unless told otherwise."""

    def __init__(self, name: str = "native", *, control: Control | None = None, failures: Iterable[str | None] = (), static_screen: bool = False, target: Target | None = None):
        super().__init__(control or Control())
        self.name = name
        self.actions: list[Action] = []
        self._failures = list(failures)
        self._static = static_screen
        self._target = target
        self.resolved: list[str] = []

    async def screenshot(self) -> bytes:
        shade = 0 if self._static else len(self.actions) * 40 % 256
        return png((255, 255 - shade, 255))

    async def _execute(self, action: Action) -> ActionOutcome:
        self.actions.append(action)
        error = self._failures.pop(0) if self._failures else None
        return ActionOutcome.failure(error) if error else ActionOutcome.success()

    async def resolve_target(self, description, hints=None):
        self.resolved.append(description)
        return self._target

    def context_hint(self):
        return "CURRENT PAGE: about:blank" if self.name == "browser" else None


class FakeFara:
    """Returns scripted actions (an exception instance is raised instead)."""

    def __init__(self, script: Iterable[Action | BaseException], verdicts: Iterable[bool] = ()):
        self._script = list(script)
        self._verdicts = list(verdicts)  # answers to verify(); True when the list runs out
        self.calls: list[dict] = []
        self.verifications: list[str] = []
        self.request_count = 0
        self.total_tokens = 0

    async def get_next_action(self, **kwargs):
        self.calls.append(kwargs)
        self.request_count += 1
        item = self._script.pop(0) if self._script else finish()
        if isinstance(item, BaseException):
            raise item
        history = list(kwargs["chat_history"]) + [
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [{"type": "text", "text": '<tool_response>\n{"status": "success"}\n</tool_response>'}]},
        ]
        return item, history

    async def verify(self, criterion, image_base64, image_media_type="image/png"):
        self.verifications.append(criterion)
        passed = self._verdicts.pop(0) if self._verdicts else True
        return Verification(passed, "It is on screen." if passed else "It is not on screen yet.")


def fara_factory(fara: FakeFara):
    async def build(config):
        return fara

    return build


def config(**overrides) -> ComputerUseConfig:
    return ComputerUseConfig(enabled=True, **overrides)


def run_graph(
    *,
    desktop,
    browser,
    fara,
    planner=None,
    prompt="goal",
    approach="plan_execute",
    execution_mode="hybrid",
    max_replans=2,
    max_steps=20,
    autonomous_max_actions=40,
    brief_model=None,
    on_plan_update=None,
    verification="fara",
    model_verifier=None,
    approval_threshold="high",
    ask=None,
    recall=None,
):
    """Run one task through the real run graph (in-memory checkpoints) with fake environments and models."""
    import asyncio

    from langgraph.checkpoint.memory import InMemorySaver

    from vilagent.agents.plan_execute import StepExecutor
    from vilagent.agents.planner import planner_context
    from vilagent.graph import build
    from vilagent.graph.state import RunContext

    context = RunContext(
        config=config(),
        desktop=desktop,
        browser=browser,
        executor=StepExecutor(config(), desktop=desktop, browser=browser, fara_factory=fara_factory(fara), verification=verification, model_verifier=model_verifier),
        planner=planner,
        brief_model=brief_model or (lambda thinking=False: None),
        planner_context=planner_context(None),
        max_steps=max_steps,
        autonomous_max_actions=autonomous_max_actions,
        on_plan_update=on_plan_update,
        approval_threshold=approval_threshold,
        ask=ask,
        recall=recall,
    )
    graph = build.build_graph(InMemorySaver())
    return asyncio.run(build.run_graph(graph, context, run_id="run-1", prompt=prompt, approach=approach, execution_mode=execution_mode, max_replans=max_replans))
