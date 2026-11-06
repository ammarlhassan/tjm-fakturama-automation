from pathlib import Path

import pytest

from image_to_cash.adapters import InjectedCrash, SimulatedAdapter
from image_to_cash.extraction import ReplayProvider
from image_to_cash.models import CompletionReceipt, Stage
from image_to_cash.service import Application
from image_to_cash.store import ConflictError
from image_to_cash.workflow import Workflow


def test_successful_retry_clears_the_resolved_provider_note(settings, monkeypatch, tmp_path):
    from types import SimpleNamespace

    service = Application(settings)
    try:
        job, _ = service.store.submit("demo", "a" * 64, str(tmp_path / "source.png"), mode="live")
        service.store.update(
            job,
            review={"code": "VISION_PROVIDER_ERROR", "message": "HTTP 503", "resolved": True},
        )
        recording = Path(__file__).resolve().parents[1] / "fixtures/sample.reviewed.json"
        fixture = ReplayProvider(recording).extract([])[0]

        class Pipeline:
            def __init__(self, *args):
                pass

            def run(self, *args):
                return SimpleNamespace(
                    extraction=fixture,
                    disagreements=[],
                    model_dump=lambda mode: {"attempts": []},
                )

        monkeypatch.setattr("image_to_cash.service.ExtractionPipeline", Pipeline)
        record = service.extract(job)
        assert record["stage"] == Stage.VALIDATED
        assert record["review"] is None
    finally:
        service.store.engine.dispose()


@pytest.mark.parametrize("point", ["set_line:0", "save_order"])
def test_review_cannot_change_reference_after_opening_an_order(settings, source, point):
    service = Application(settings)
    try:
        job, _ = service.store.submit("demo", "a" * 64, "source.png", mode="simulation")
        service.store.update(job, source=source.model_dump(mode="json"), stage=Stage.VALIDATED)
        service.store.check_reference(
            job, source.external_reference, source.model_dump(mode="json")
        )
        path = settings.workspace_path("demo") / "simulation.json"
        with pytest.raises(InjectedCrash):
            Workflow(settings, service.store, SimulatedAdapter(path, crash_at=point)).run(job)
        service.store.update(job, stage=Stage.NEEDS_REVIEW, review={"code": "UI_OUTCOME_UNKNOWN"})
        changed = source.model_copy(deep=True)
        changed.external_reference = "CORRECTED-REFERENCE"

        with pytest.raises(ConflictError, match="reference"):
            service.review(job, changed, "Corrected external reference")
        assert service.store.get(job)["source"] == source.model_dump(mode="json")
        assert SimulatedAdapter(path).state["calls"].count("open_order") == 1
        assert service.store.worker_health()["workspaces"][0]["quarantined"]
    finally:
        service.store.engine.dispose()


def test_reference_correction_before_desktop_releases_the_old_reservation(settings, source):
    service = Application(settings)
    try:
        job, _ = service.store.submit("demo", "a" * 64, "first.png", mode="simulation")
        service.store.update(
            job,
            source=source.model_dump(mode="json"),
            stage=Stage.NEEDS_REVIEW,
            review={"code": "DESKTOP_UNAVAILABLE"},
        )
        service.store.check_reference(
            job, source.external_reference, source.model_dump(mode="json")
        )
        corrected = source.model_copy(deep=True)
        corrected.external_reference = "CORRECTED-REFERENCE"
        service.review(job, corrected, "Corrected reference before desktop execution")

        second, _ = service.store.submit("demo", "b" * 64, "second.png", mode="simulation")
        assert (
            service.store.check_reference(
                second, source.external_reference, source.model_dump(mode="json")
            )
            is None
        )
        assert service.store.get(job)["external_reference"] == corrected.external_reference
        assert service.store.get(second)["external_reference"] == source.external_reference
    finally:
        service.store.engine.dispose()


def test_completed_live_receipt_is_available_after_desktop_shutdown(settings, source, monkeypatch):
    service = Application(settings)
    try:
        job, _ = service.store.submit("demo", "a" * 64, "source.png", mode="live")
        receipt = CompletionReceipt(
            job_id=job,
            workspace="demo",
            mode="live",
            order_number="TEST-ORDER",
            invoice_number="TEST-INVOICE",
            external_reference=source.external_reference,
            totals=source.totals,
            payment_method="Bank Transfer",
            paid=True,
        )
        service.store.update(job, stage=Stage.SUCCEEDED, receipt=receipt.model_dump(mode="json"))

        def unavailable(*args, **kwargs):
            raise AssertionError("A completed receipt must not initialize the desktop")

        monkeypatch.setattr("image_to_cash.windows.WindowsAdapter", unavailable)
        assert service.execute(job) == receipt
    finally:
        service.store.engine.dispose()


def test_reviewed_duplicate_reuses_verified_receipt_without_desktop(settings, source, monkeypatch):
    service = Application(settings)
    try:
        first, _ = service.store.submit("demo", "a" * 64, "first.png", mode="simulation")
        service.store.update(first, source=source.model_dump(mode="json"), stage=Stage.VALIDATED)
        service.store.check_reference(
            first, source.external_reference, source.model_dump(mode="json")
        )
        path = settings.workspace_path("demo") / "simulation.json"
        original = Workflow(settings, service.store, SimulatedAdapter(path)).run(first)
        assert original is not None
        second, _ = service.store.submit("demo", "b" * 64, "second.png", mode="simulation")
        wrong = source.model_copy(deep=True)
        wrong.items[0].sku = "OCR-ERROR"
        service.store.update(
            second,
            source=wrong.model_dump(mode="json"),
            stage=Stage.NEEDS_REVIEW,
            review={"code": "EXTRACTION_REVIEW", "resolved": False},
        )

        def unavailable(*args, **kwargs):
            raise AssertionError("A corrected duplicate must not initialize the desktop")

        monkeypatch.setattr(service, "_adapter", unavailable)
        record = service.review(second, source, "Corrected SKU against the visible image")
        assert record["stage"] == Stage.SUCCEEDED
        assert record["receipt"]["job_id"] == second
        assert record["receipt"]["order_number"] == original.order_number
        assert record["receipt"]["invoice_number"] == original.invoice_number
        assert service.queue_resume(second)["stage"] == Stage.SUCCEEDED
        assert service.execute(second).order_number == original.order_number
        events = service.store.events(second)
        assert events[-1]["action"] == "duplicate_reference"
        assert events[-1]["data"]["original_job_id"] == first
        correction = next(event for event in events if event["action"] == "human_review")
        assert correction["data"]["before"]["items"][0]["sku"] == "OCR-ERROR"
        assert correction["data"]["after"] == source.model_dump(mode="json")
        assert service.store.worker_health()["workspaces"] == []
        assert len(SimulatedAdapter(path).state["documents"]) == 2
    finally:
        service.store.engine.dispose()
