"""Tests for fail-closed Windows screenshot redaction."""

from __future__ import annotations

import pytest
from PIL import Image

from vilagent.computer_use.models import Rect
from vilagent.computer_use.windows.redaction import RedactionUnavailableError, WindowsUIAPasswordRedactor


def test_password_redactor_masks_only_reported_regions():
    image = Image.new("RGB", (10, 10), color="white")
    redactor = WindowsUIAPasswordRedactor(region_reader=lambda: [Rect(x=2, y=3, width=4, height=2)])

    redacted = redactor(image)

    assert redacted.getpixel((2, 3)) == (0, 0, 0)
    assert redacted.getpixel((5, 4)) == (0, 0, 0)
    assert redacted.getpixel((1, 3)) == (255, 255, 255)
    assert image.getpixel((2, 3)) == (255, 255, 255)


def test_password_redactor_fails_closed_when_region_scan_fails():
    def fail():
        raise RuntimeError("uia unavailable")

    with pytest.raises(RedactionUnavailableError):
        WindowsUIAPasswordRedactor(region_reader=fail)(Image.new("RGB", (10, 10), color="white"))


def test_password_redactor_queries_only_edit_descendants():
    queries = []

    class Info:
        is_password = False

    class Window:
        element_info = Info()

        def descendants(self, **kwargs):
            queries.append(kwargs)
            return []

    controls = WindowsUIAPasswordRedactor._candidate_controls(Window())

    assert len(controls) == 1
    assert queries == [{"control_type": "Edit"}]
