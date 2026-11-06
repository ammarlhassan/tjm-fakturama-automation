from decimal import Decimal
from pathlib import Path

import pytest

from image_to_cash.models import ExtractionResult
from image_to_cash.ocr import LayoutParser, OCRDocument, compare_readings, number


def test_recorded_local_ocr_parses_visible_sample(source):
    path = Path(__file__).parents[1] / "fixtures/sample.ocr.json"
    document = OCRDocument.model_validate_json(path.read_text(encoding="utf-8"))
    result = LayoutParser(document).parse()
    assert compare_readings(result, ExtractionResult(order=source)) == []


def test_matching_totals_cannot_hide_wrong_sku(source):
    other = source.model_copy(deep=True)
    other.items[0].sku = "CHR-ERG-O1"
    discrepancies = compare_readings(ExtractionResult(order=source), ExtractionResult(order=other))
    assert any(d["field"] == "items.0.sku" for d in discrepancies)


@pytest.mark.parametrize(
    "text,expected",
    [("1.234,56 EUR", "1234.56"), ("1,234.56", "1234.56"), ("19%", "19"), ("0,50", "0.50")],
)
def test_explicit_numeric_formats(text, expected):
    assert number(text) == Decimal(expected)


def test_ambiguous_number_not_silently_repaired():
    with pytest.raises(ValueError):
        number("1,234")
