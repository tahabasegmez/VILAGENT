"""Tests for vision image downscaling + coordinate round-trip.

The remote-model send downscales the screenshot; these lock the scale factor so
model coordinates always map back to the right screen pixels (no misclicks).
"""

from __future__ import annotations

import io

from vilagent.computer_use.image_ops import encode_image_for_vision, scale_point
from vilagent.computer_use.models import ActionCommand, ActionKind, Rect, TargetRef, TargetStrategy
from vilagent.computer_use.plan_execute import _rescale_action_for_screen


def _png(width: int, height: int) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (240, 240, 240)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_small_image_is_jpeg_without_downscale():
    _, media_type, scale = encode_image_for_vision(_png(800, 600), max_dim=1280)
    assert media_type == "image/jpeg"
    assert scale == 1.0


def test_large_image_downscaled_with_correct_scale_and_round_trip():
    _, media_type, scale = encode_image_for_vision(_png(2000, 1000), max_dim=1280, jpeg_quality=80)
    assert media_type == "image/jpeg"
    assert round(scale, 4) == round(2000 / 1280, 4)  # 1.5625
    # The center of the sent image must map back to the center of the screen.
    sent_w, sent_h = 1280, round(1000 * (1280 / 2000))
    sx, sy = scale_point((sent_w / 2, sent_h / 2), scale)
    assert abs(sx - 1000) <= 2
    assert abs(sy - 500) <= 2


def test_max_dim_zero_disables_downscale():
    _, _, scale = encode_image_for_vision(_png(4000, 2000), max_dim=0)
    assert scale == 1.0


def test_invalid_bytes_falls_back_to_png():
    _, media_type, scale = encode_image_for_vision(b"not-an-image", max_dim=1280)
    assert media_type == "image/png"
    assert scale == 1.0


def test_rescale_action_scales_coordinate_target_and_drag_end():
    action = ActionCommand(
        action_id="a",
        session_id="",
        kind=ActionKind.drag,
        target=TargetRef(strategy=TargetStrategy.coordinate, selector={"point": [100, 200]}, bounds=Rect(x=100, y=200, width=1, height=1), confidence=1, observation_id=""),
        args={"end": [300, 400]},
    )
    scaled = _rescale_action_for_screen(action, 2.0)
    assert scaled.target.selector["point"] == [200, 400]
    assert scaled.target.bounds.x == 200 and scaled.target.bounds.y == 400
    assert scaled.args["end"] == [600, 800]


def test_rescale_leaves_non_coordinate_targets_untouched():
    action = ActionCommand(
        action_id="a",
        session_id="",
        kind=ActionKind.click,
        target=TargetRef(strategy=TargetStrategy.uia, selector={"automation_id": "save"}, confidence=1, observation_id=""),
    )
    assert _rescale_action_for_screen(action, 2.0) is action
