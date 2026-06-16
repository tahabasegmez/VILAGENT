"""Fail-closed Windows screenshot redaction."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vilagent.computer_use.models import Rect
from vilagent.computer_use.windows.uia import UIAUnavailableError, prepare_comtypes_cache


class RedactionUnavailableError(RuntimeError):
    """Raised when sensitive regions cannot be determined safely."""

    reason_code = "redaction_unavailable"


class WindowsUIAPasswordRedactor:
    """Mask UIA controls explicitly marked as password fields."""

    def __init__(
        self,
        *,
        region_reader: Callable[[], list[Rect]] | None = None,
        comtypes_cache_dir: str | None = None,
    ):
        self._region_reader = region_reader or self._read_password_regions
        self._comtypes_cache_dir = comtypes_cache_dir

    def __call__(self, image: Any) -> Any:
        try:
            regions = self._region_reader()
            redacted = image.copy()
            from PIL import ImageDraw

            draw = ImageDraw.Draw(redacted)
            for region in regions:
                left = max(0, region.x)
                top = max(0, region.y)
                right = min(redacted.width, region.x + region.width)
                bottom = min(redacted.height, region.y + region.height)
                if left < right and top < bottom:
                    draw.rectangle((left, top, right - 1, bottom - 1), fill="black")
            return redacted
        except RedactionUnavailableError:
            raise
        except Exception as exc:
            raise RedactionUnavailableError("Sensitive-region redaction failed") from exc

    def _read_password_regions(self) -> list[Rect]:
        try:
            prepare_comtypes_cache(self._comtypes_cache_dir)
            from pywinauto import Desktop

            regions: list[Rect] = []
            for window in Desktop(backend="uia").windows():
                # IsPassword applies to text-entry controls. Restricting this
                # query avoids traversing every desktop control per screenshot.
                for control in self._candidate_controls(window):
                    info = getattr(control, "element_info", None)
                    if not bool(getattr(info, "is_password", False)):
                        continue
                    rectangle = control.rectangle()
                    width = int(rectangle.right - rectangle.left)
                    height = int(rectangle.bottom - rectangle.top)
                    if width > 0 and height > 0:
                        regions.append(Rect(x=int(rectangle.left), y=int(rectangle.top), width=width, height=height))
            return regions
        except Exception as exc:
            raise RedactionUnavailableError("Unable to enumerate sensitive UIA regions") from exc

    @staticmethod
    def _candidate_controls(window: Any) -> list[Any]:
        return [window, *window.descendants(control_type="Edit")]
