from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from . import __version__
from .config import Settings


def source_identity() -> dict:
    """Identify the package source behind a run, stable across checkouts and line endings."""
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in {".py", ".html"})
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return {"package_version": __version__, "source_sha256": digest.hexdigest()}


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=False, indent=2, default=str)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def ingest(data: bytes, settings: Settings) -> tuple[str, Path]:
    import io

    if len(data) > settings.max_upload_bytes:
        raise ValueError("Image exceeds upload size limit")
    digest = hashlib.sha256(data).hexdigest()
    try:
        opened = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Submit a valid raster order image") from exc
    with opened as image:
        if image.format not in {"PNG", "JPEG", "TIFF", "WEBP", "BMP"}:
            raise ValueError("Submit a raster order image, not a PDF or other document")
        if image.width * image.height > settings.max_image_pixels:
            raise ValueError("Image exceeds decoded pixel limit")
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("Only a single order image is supported")
        try:
            oriented = ImageOps.exif_transpose(image)
            # Dropping alpha directly can expose originally invisible RGB text.
            transparent = "A" in oriented.getbands() or "transparency" in oriented.info
            normalized = (
                Image.alpha_composite(
                    Image.new("RGBA", oriented.size, "white"), oriented.convert("RGBA")
                ).convert("RGB")
                if transparent
                else oriented.convert("RGB")
            )
        except (OSError, ValueError) as exc:
            raise ValueError("Raster image could not be fully decoded") from exc
        folder = settings.runtime / "cache" / "images" / digest
        if transparent:
            folder = folder / "visible-alpha-white-v1"
        folder.mkdir(parents=True, exist_ok=True)
        original = folder / "original.bin"
        if not original.exists():
            original.write_bytes(data)
        path = folder / "source.png"
        if not path.exists():
            normalized.save(path)
    return digest, path
