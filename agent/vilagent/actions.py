"""The action vocabulary shared by the vision model, the agents and the environments."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ActionKind(StrEnum):
    # Pointer actions (need a target).
    click = "click"
    double_click = "double_click"
    right_click = "right_click"
    # Target optional.
    scroll = "scroll"
    type_text = "type_text"
    hotkey = "hotkey"
    launch_app = "launch_app"
    focus_window = "focus_window"
    # Browser navigation: args.action is visit_url | web_search | history_back | go_forward | refresh.
    browser_action = "browser_action"
    # Model-loop signals: never executed by an environment.
    wait = "wait"
    mouse_move = "mouse_move"
    finish = "finish"


POINTER_KINDS = frozenset({ActionKind.click, ActionKind.double_click, ActionKind.right_click})
LOOP_SIGNALS = frozenset({ActionKind.wait, ActionKind.mouse_move, ActionKind.finish})


class TargetStrategy(StrEnum):
    coordinate = "coordinate"  # a screen/page pixel chosen by the vision model
    uia = "uia"  # a Windows UI Automation element resolved from the accessibility tree


class Rect(BaseModel):
    x: int
    y: int
    width: int = Field(ge=1)
    height: int = Field(ge=1)

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2


class Target(BaseModel):
    strategy: TargetStrategy
    point: tuple[int, int] | None = None
    selector: dict[str, Any] = Field(default_factory=dict)
    bounds: Rect | None = None

    @classmethod
    def at(cls, x: int, y: int) -> Target:
        return cls(strategy=TargetStrategy.coordinate, point=(int(x), int(y)))

    @property
    def center(self) -> tuple[int, int] | None:
        if self.point is not None:
            return self.point
        return self.bounds.center if self.bounds is not None else None


class Action(BaseModel):
    kind: ActionKind
    target: Target | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    # The model's one-line narration for this action (shown live in the UI).
    thought: str | None = None

    @model_validator(mode="after")
    def _pointer_needs_target(self) -> Action:
        if self.kind in POINTER_KINDS and self.target is None:
            raise ValueError(f"'{self.kind}' needs a target")
        return self

    @property
    def point(self) -> tuple[int, int] | None:
        return self.target.center if self.target is not None else None


class ActionOutcome(BaseModel):
    ok: bool
    error: str | None = None

    @classmethod
    def success(cls) -> ActionOutcome:
        return cls(ok=True)

    @classmethod
    def failure(cls, error: str) -> ActionOutcome:
        return cls(ok=False, error=error)
