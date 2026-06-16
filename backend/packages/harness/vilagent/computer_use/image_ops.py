"""Image preparation for remote vision models.

The local observation store keeps full-resolution PNG screenshots (needed for
verification, redaction, and audit). Sending that full PNG to a remote vision
model on every action is the dominant latency cost, so for the model send we
downscale to a bounded dimension and re-encode as JPEG.

Returns the scale factor from *sent* pixels back to *screen* pixels so the
caller can map model coordinates back to the real desktop without error.
"""

from __future__ import annotations

import base64
import io


def encode_image_for_vision(png_bytes: bytes, *, max_dim: int = 1280, jpeg_quality: int = 85) -> tuple[str, str, float]:
    """Downscale + JPEG-encode a screenshot for a remote vision model.

    Args:
        png_bytes: full-resolution screenshot bytes (any PIL-readable format).
        max_dim: longest-edge cap in pixels; ``<= 0`` disables downscaling.
        jpeg_quality: JPEG quality (1-95).

    Returns:
        ``(base64_payload, media_type, scale)`` where ``scale`` multiplies a
        coordinate expressed in the *sent* image back to *screen* pixels
        (``screen = sent * scale``). With no downscaling ``scale`` is ``1.0``.
        Falls back to the original PNG (scale 1.0) if PIL or encoding fails.
    """
    # max_dim <= 0 is a true no-op: send the original screenshot untouched so the
    # vision model sees exactly the screen pixels and coordinates map 1:1. This is
    # the safe default — downscaling is opt-in because some action models do not
    # return coordinates in the literal sent-image pixel space.
    if not max_dim or max_dim <= 0:
        return base64.b64encode(png_bytes).decode("ascii"), "image/png", 1.0

    try:
        from PIL import Image

        with Image.open(io.BytesIO(png_bytes)) as opened:
            image = opened.convert("RGB")
            width, height = image.size
            scale = 1.0
            longest = max(width, height)
            if max_dim and max_dim > 0 and longest > max_dim:
                ratio = max_dim / float(longest)
                new_size = (max(1, round(width * ratio)), max(1, round(height * ratio)))
                image = image.resize(new_size, Image.LANCZOS)
                # screen = sent / ratio
                scale = 1.0 / ratio
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=jpeg_quality)
            payload = buffer.getvalue()
        return base64.b64encode(payload).decode("ascii"), "image/jpeg", scale
    except Exception:
        # Never let image optimization break the run: fall back to the raw PNG.
        return base64.b64encode(png_bytes).decode("ascii"), "image/png", 1.0


def scale_point(point: tuple[float, float] | list[float], scale: float) -> tuple[int, int]:
    """Map a sent-image coordinate back to screen pixels."""
    return int(round(point[0] * scale)), int(round(point[1] * scale))
