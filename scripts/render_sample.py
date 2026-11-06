"""Render visible pixels only; never read the PDF text layer.

The crop is specific to the supplied assignment fixture, never to a production image or UI.
"""

from pathlib import Path

import pypdfium2 as pdfium

from image_to_cash.artifacts import write_json

root = Path(__file__).resolve().parents[1]
source = root / "[EXTERNAL] Take home project 2026.pdf"
document = pdfium.PdfDocument(str(source))
page = document[2]
bitmap = page.render(scale=4)
image = bitmap.to_pil()
box = (646, 404, 1802, 2024)
image.crop(box).save(root / "fixtures/sample.visible.png")
write_json(
    root / "fixtures/sample.provenance.json",
    {
        "source": source.name,
        "page": 3,
        "render_scale": 4,
        "crop_pixels": box,
        "text_layer_used": False,
        "visual_review": "Visible crop checked against page 3",
    },
)
bitmap.close()
page.close()
document.close()
print("Rendered visible source image from PDF page 3")
