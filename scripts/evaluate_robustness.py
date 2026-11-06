"""Separate negative cases and transformed variants; never inflate corpus size."""

from __future__ import annotations

import argparse
import io
import time
from decimal import Decimal
from pathlib import Path

from generate_fixtures import render
from PIL import Image, ImageFilter

from image_to_cash.artifacts import write_json
from image_to_cash.config import Settings
from image_to_cash.evaluation import differences
from image_to_cash.models import OrderInput
from image_to_cash.ocr import LayoutParser, recognize
from image_to_cash.validation import validate_order

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Also evaluate real hosted vision; consumes API quota"
    )
    options = parser.parse_args()
    settings = Settings()
    pipeline = None
    if options.live:
        from image_to_cash.extraction import ExtractionPipeline

        pipeline = ExtractionPipeline(settings)
    output = ROOT / "artifacts/robustness"
    output.mkdir(parents=True, exist_ok=True)
    source = OrderInput.model_validate_json(
        (ROOT / "fixtures/sample.json").read_text(encoding="utf-8")
    )
    cases = []
    original = Image.open(ROOT / "fixtures/sample.visible.png").convert("RGB")
    compressed = io.BytesIO()
    original.save(compressed, format="JPEG", quality=35)
    for name, image in (
        (
            "downsampled-75-percent",
            original.resize((original.width * 3 // 4, original.height * 3 // 4)),
        ),
        ("rotated-1-degree", original.rotate(1, expand=True, fillcolor="white")),
        ("gaussian-blur-1", original.filter(ImageFilter.GaussianBlur(1))),
        ("jpeg-quality-35", Image.open(io.BytesIO(compressed.getvalue())).convert("RGB")),
    ):
        path = output / f"{name}.png"
        image.save(path)
        cases.append((name, path, source, "transformed_variant", None))
    for name, field, expected_code in (
        ("paid-without-date", "date", "PAYMENT_DATE_MISSING"),
        ("missing-sku", "sku", "MISSING_FIELD"),
        ("incorrect-printed-total", "total", "TOTAL_MISMATCH"),
    ):
        order = source.model_copy(deep=True)
        if field == "date":
            order.payment.payment_date = None
        elif field == "sku":
            order.items[0].sku = None
        else:
            order.totals.gross += Decimal("0.01")
        path = output / f"{name}.png"
        render(order, path)
        cases.append((name, path, order, "negative_case", expected_code))
    rows = []
    for name, path, expected, category, reason in cases:
        started = time.perf_counter()
        ocr = recognize(path, settings)
        reading = LayoutParser(ocr).parse()
        validation = validate_order(reading.order)
        delta = differences(expected, reading.order)
        rows.append(
            {
                "name": name,
                "category": category,
                "base_document": "official-visible-sample",
                "image": path.name,
                "expected_review_code": reason,
                "exact_ocr": not delta,
                "differences": delta,
                "validation": validation.model_dump(mode="json"),
                "expected_source_validation": validate_order(expected).model_dump(mode="json"),
                "seconds": time.perf_counter() - started,
            }
        )
        if pipeline:
            result = pipeline.run(path, output / name)
            delta = differences(expected, result.extraction.order)
            accepted = not result.disagreements and validate_order(result.extraction.order).accepted
            rows[-1]["pipeline"] = {
                "accepted": accepted,
                "exact": not delta,
                "incorrect_acceptance": accepted
                and (bool(delta) or not validate_order(expected).accepted),
                "differences": delta,
                "review_findings": result.disagreements,
                "attempts": result.attempts,
            }
        write_json(
            output / f"{name}-reading.json",
            {
                "ocr": ocr.model_dump(mode="json"),
                "extraction": reading.model_dump(mode="json"),
                "expected": expected.model_dump(mode="json"),
            },
        )
    result = {
        "mode": "live_pipeline" if pipeline else "local_ocr_only",
        "hosted_vision": pipeline.provider.identity if pipeline else "not measured",
        "independent_documents_added_to_accuracy_corpus": 0,
        "note": "Four transformations of one source plus three separate negative cases. These do not establish full pipeline acceptance or real-world accuracy.",
        "cases": rows,
    }
    write_json(output / "results.json", result)
    print(
        {
            "cases": len(rows),
            "exact_ocr": sum(row["exact_ocr"] for row in rows),
            "output": str(output),
        }
    )


if __name__ == "__main__":
    main()
