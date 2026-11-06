import json
from pathlib import Path

import httpx
import pytest

from image_to_cash.extraction import GeminiProvider, ReplayProvider
from image_to_cash.models import ReviewRequired


def test_replay_is_labelled_and_has_no_usage():
    path = Path(__file__).parents[1] / "fixtures/sample.reviewed.json"
    result, metadata = ReplayProvider(path).extract([])
    assert result.order.external_reference == "WEB-2026-0714-A17"
    assert metadata["provider"] == "replay"
    assert "NOT an API recording" in metadata["origin"]
    assert metadata["usage"] == {}


def test_gemini_request_uses_independent_image_and_schema(monkeypatch, settings, source):
    from image_to_cash.models import ExtractionResult

    monkeypatch.setenv("GEMINI_API_KEY", "test-only-placeholder")
    image = Path(__file__).parents[1] / "fixtures/sample.visible.png"
    response_data = {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {"parts": [{"text": ExtractionResult(order=source).model_dump_json()}]},
            }
        ],
        "usageMetadata": {"totalTokenCount": 100},
    }

    def post(self, url, **kwargs):
        assert kwargs["headers"]["x-goog-api-key"] == "test-only-placeholder"
        body = kwargs["json"]
        assert body["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "image/png"
        assert "responseJsonSchema" in body["generationConfig"]
        assert "Northstar" not in json.dumps(body["contents"][0]["parts"][0])
        return httpx.Response(200, json=response_data)

    monkeypatch.setattr(httpx.Client, "post", post)
    result, metadata = GeminiProvider(settings).extract([image])
    assert result.order == source
    assert metadata["provider"] == "gemini"


@pytest.mark.parametrize("network_failure", [False, True])
def test_failed_transport_attempts_are_bounded_audited_and_do_not_echo_keys(
    monkeypatch, settings, network_failure
):
    marker = "test-only-secret-placeholder"
    monkeypatch.setenv("GEMINI_API_KEY", marker)
    monkeypatch.setattr("image_to_cash.extraction.time.sleep", lambda _: None)

    def post(*args, **kwargs):
        if network_failure:
            raise httpx.ConnectError(marker)
        return httpx.Response(503, json={"error": marker}, headers={"retry-after": "HTTP-date"})

    monkeypatch.setattr(httpx.Client, "post", post)
    with pytest.raises(ReviewRequired) as raised:
        GeminiProvider(settings).extract([])
    attempts = raised.value.evidence["transport_attempts"]
    assert [attempt["attempt"] for attempt in attempts] == [1, 2, 3]
    assert marker not in str(raised.value) + json.dumps(raised.value.evidence)


@pytest.mark.parametrize(
    ("status", "advice", "misleading"),
    [
        (503, "retry later", "key"),
        (403, "verify model access, key", "retry later"),
    ],
)
def test_provider_error_advice_matches_the_status(
    monkeypatch, settings, status, advice, misleading
):
    monkeypatch.setenv("GEMINI_API_KEY", "test-only-placeholder")
    monkeypatch.setattr("image_to_cash.extraction.time.sleep", lambda _: None)
    monkeypatch.setattr(httpx.Client, "post", lambda *args, **kwargs: httpx.Response(status))
    with pytest.raises(ReviewRequired) as raised:
        GeminiProvider(settings).extract([])
    assert raised.value.code == "VISION_PROVIDER_ERROR"
    assert advice in str(raised.value)
    assert misleading not in str(raised.value)


def test_a_reused_reading_is_marked_cached_and_makes_no_provider_call(
    monkeypatch, settings, source, tmp_path
):
    from image_to_cash.extraction import ExtractionPipeline
    from image_to_cash.models import ExtractionResult
    from image_to_cash.ocr import OCRDocument

    calls = []

    class Provider:
        identity = "test-double"

        def extract(self, images):
            calls.append(images)
            # No field evidence, so the first reading always triggers the repair pass.
            return ExtractionResult(order=source), {"provider": "test-double", "call": len(calls)}

    class Parser:
        def __init__(self, ocr):
            pass

        def parse(self):
            return ExtractionResult(order=source)

    monkeypatch.setattr(
        "image_to_cash.extraction.recognize",
        lambda *args, **kwargs: OCRDocument(
            engine="test-double", version="1", width=1, height=1, elapsed_seconds=0
        ),
    )
    monkeypatch.setattr("image_to_cash.extraction.LayoutParser", Parser)
    image = Path(__file__).parents[1] / "fixtures/sample.visible.png"
    pipeline = ExtractionPipeline(settings, provider=Provider())

    fresh = pipeline.run(image, tmp_path / "first")
    assert len(calls) == 2
    assert [attempt.get("cache_hit") for attempt in fresh.attempts] == [None, None]
    assert fresh.first_pass_findings

    reused = pipeline.run(image, tmp_path / "second")
    assert len(calls) == 2
    assert [attempt.get("cache_hit") for attempt in reused.attempts] == [True, True]
    assert [attempt["call"] for attempt in reused.attempts] == [1, 2]


def test_matching_values_without_evidence_cannot_be_accepted(source):
    from image_to_cash.models import ExtractionResult, FieldEvidence
    from image_to_cash.validation import evidence_findings, flatten

    reading = ExtractionResult(order=source)
    findings = evidence_findings(reading, "vision")
    assert any(item["field"] == "items.0.sku" for item in findings)
    assert any(item["field"] == "customer.billing.postcode" for item in findings)
    reading.evidence = [
        FieldEvidence(field=name, raw_text=str(value), engine="reviewed-fixture")
        for name, value in flatten(source).items()
        if value is not None
    ]
    assert evidence_findings(reading, "vision") == []


def test_schema_relative_evidence_paths_normalize_without_changing_literal_values(source):
    from image_to_cash.extraction import normalize_evidence_paths
    from image_to_cash.models import ExtractionResult, FieldEvidence

    reading = ExtractionResult(
        order=source,
        evidence=[
            FieldEvidence(field="order.items[0].sku", raw_text="CHR-ERG-01", engine="vision"),
            FieldEvidence(
                field="order.customer.billing.postcode", raw_text="10117", engine="vision"
            ),
            FieldEvidence(field="order.unknown", raw_text="invented", engine="vision"),
        ],
    )
    before = reading.order.model_dump_json()
    changes = normalize_evidence_paths(reading)
    assert [item.field for item in reading.evidence] == [
        "items.0.sku",
        "customer.billing.postcode",
        "order.unknown",
    ]
    assert len(changes) == 2
    assert reading.evidence[0].raw_text == "CHR-ERG-01"
    assert reading.order.model_dump_json() == before
