import io

import pytest
from PIL import Image

from image_to_cash.artifacts import ingest


def test_fully_transparent_pixels_do_not_become_visible_ink(settings):
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    image.putpixel((50, 50), (0, 0, 0, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    _, path = ingest(buffer.getvalue(), settings)
    with Image.open(path) as actual:
        assert actual.getpixel((0, 0)) == (255, 255, 255)
        assert actual.getpixel((50, 50)) == (0, 0, 0)


def test_truncated_image_with_valid_header_is_rejected_before_job_creation(settings):
    buffer = io.BytesIO()
    Image.new("RGB", (200, 200), "white").save(buffer, format="PNG")
    payload = buffer.getvalue()
    with pytest.raises(ValueError, match="decoded"):
        ingest(payload[: len(payload) // 2], settings)
