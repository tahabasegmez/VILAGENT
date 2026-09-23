"""Tests for vision image downscaling + coordinate round-trip.

The remote-model send downscales the screenshot; these lock the scale factor so
model coordinates always map back to the right screen pixels (no misclicks).
"""

from __future__ import annotations

import io

from vilagent.actions import Action, ActionKind, Target, TargetStrategy
from vilagent.agents.vision_loop import rescale_action
from vilagent.vision.image_ops import encode_image_for_vision, scale_point, screen_changed


def _png(width: int, height: int, color=(240, 240, 240)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
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


def test_rescale_action_scales_coordinate_target():
    action = Action(kind=ActionKind.click, target=Target.at(100, 200))

    assert rescale_action(action, 2.0).target.point == (200, 400)


def test_rescale_leaves_non_coordinate_targets_untouched():
    action = Action(kind=ActionKind.click, target=Target(strategy=TargetStrategy.uia, selector={"automation_id": "save"}))

    assert rescale_action(action, 2.0) is action


def test_screen_changed_detects_visible_difference_only():
    white = _png(200, 100, (255, 255, 255))

    assert screen_changed(white, white) is False
    assert screen_changed(white, _png(200, 100, (254, 255, 255))) is False
    assert screen_changed(white, _png(200, 100, (0, 0, 0))) is True
    assert screen_changed(white, _png(100, 100, (255, 255, 255))) is True
