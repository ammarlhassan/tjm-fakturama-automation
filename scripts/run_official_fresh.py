"""Run the unchanged official image with live extraction through a brand-new workspace.

The workspace starts with no Debtor, payment method, VAT rate or Product, so this exercises
every creation branch in the brief. Fakturama's validator leaves the reserved .test email
blank on the new contact; the run must finish and flag that. Any other stop is reported as is.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path

from acceptance_common import (
    RETRY_REASON,
    awaiting_provider_retry,
    extract_bounded,
    review_counts,
)
from complete_official_sample import acceptance

from image_to_cash.artifacts import source_identity, write_json
from image_to_cash.evaluation import differences
from image_to_cash.models import CompletionReceipt, OrderInput, Stage
from image_to_cash.service import Application, public_job

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_IMAGE_SHA256 = "7483f718cfefb9f1ca33b07e3614886ef56a47ed6aae71cde81a5c1742588833"
REQUIRED_CREATIONS = {"begin_debtor", "create_payment", "create_debtor", "create_vat"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="official-fresh")
    parser.add_argument("--output", default="artifacts/official-sample-live")
    parser.add_argument("--provider-attempts", type=int, default=6)
    parser.add_argument("--provider-wait-seconds", type=int, default=150)
    options = parser.parse_args()
    output = ROOT / options.output
    image = ROOT / "fixtures/sample.visible.png"
    data = image.read_bytes()
    if hashlib.sha256(data).hexdigest() != OFFICIAL_IMAGE_SHA256:
        raise RuntimeError("fixtures/sample.visible.png is not the unchanged official image")
    fixture = OrderInput.model_validate_json(
        (ROOT / "fixtures/sample.json").read_text(encoding="utf-8")
    )
    service = Application()
    try:
        job_id, duplicate = service.submit(data, options.workspace, mode="live", execute=False)
        print(
            json.dumps({"job_id": job_id, "duplicate": duplicate, "phase": "extraction"}),
            flush=True,
        )
        started = time.perf_counter()
        record = service.store.get(job_id)
        already_complete = record["stage"] == Stage.SUCCEEDED
        if awaiting_provider_retry(record):
            service.review(job_id, None, RETRY_REASON)
            service.queue_resume(job_id)
        record = extract_bounded(
            service,
            job_id,
            attempts=options.provider_attempts,
            wait_seconds=options.provider_wait_seconds,
        )
        extraction_seconds = time.perf_counter() - started
        print(
            json.dumps({"stage": record["stage"], "review": record["review"]}, default=str),
            flush=True,
        )
        if record["stage"] not in {Stage.VALIDATED, Stage.SUCCEEDED}:
            raise RuntimeError(f"Extraction stopped for review: {record['review']}")
        source = OrderInput.model_validate(record["source"])
        mismatches = differences(fixture, source)
        if mismatches:
            raise RuntimeError(f"Live extraction differs from the visible sample: {mismatches}")
        print(json.dumps({"phase": "desktop"}), flush=True)
        desktop_started = time.perf_counter()
        service.execute(job_id)
        desktop_seconds = time.perf_counter() - desktop_started
        record = service.store.get(job_id)
        print(
            json.dumps({"stage": record["stage"], "review": record["review"]}, default=str),
            flush=True,
        )
        service.report(job_id)
        output.mkdir(parents=True, exist_ok=True)
        shutil.copytree(service.settings.runtime / "jobs" / job_id, output, dirs_exist_ok=True)
        events = service.store.events(job_id)
        write_json(output / "journal.json", events)
        write_json(output / "job.json", public_job(record))
        if record["stage"] != Stage.SUCCEEDED:
            raise RuntimeError(record["review"])
        repeated, deduplicated = service.submit(data, options.workspace, mode="live", execute=False)
        repeat_receipt = service.execute(job_id)
        receipt = CompletionReceipt.model_validate(record["receipt"])
        documents = json.loads((output / "observed-documents.json").read_text(encoding="utf-8"))
        observed = {event["action"] for event in events if event["status"] == "observed"}
        products = sum(
            event["action"] == "create_product" and event["status"] == "observed"
            for event in events
        )
        attempts = record["extraction"]["attempts"]
        checks = acceptance(source, receipt, documents)
        checks["checks"].update(
            created_debtor_payment_vat=REQUIRED_CREATIONS.issubset(observed),
            created_two_products=products == 2,
            blank_email_flagged=receipt.warnings[0].persisted_state == "blank"
            and receipt.warnings[0].persisted_value is None,
            live_extraction_not_replay=all(a.get("provider") != "replay" for a in attempts),
            no_operator_reviews=review_counts(events)["operator_reviews"] == 0,
        )
        checks["passed"] = all(checks["checks"].values())
        reused = sum(bool(a.get("cache_hit")) for a in attempts)
        readings = "live vision" if not reused else "reused vision responses"
        result = {
            "job_id": job_id,
            "status": record["stage"],
            "mode": f"{readings} + local OCR + real Fakturama, brand-new workspace",
            "fresh_provider_calls": len(attempts) - reused,
            "reused_provider_responses": reused,
            "official_sample": True,
            "source_changes": [],
            "image_sha256": OFFICIAL_IMAGE_SHA256,
            **review_counts(events),
            "extraction_wall_seconds_including_provider_waits": None
            if already_complete
            else extraction_seconds,
            "desktop_seconds": None if already_complete else desktop_seconds,
            "vision_cache_hit": any(a.get("cache_hit") for a in attempts),
            "acceptance": checks,
            "duplicate_submission": {
                "same_job": repeated == job_id,
                "deduplicated": deduplicated,
                "same_receipt": repeat_receipt == receipt,
            },
            "warnings": [warning.model_dump(mode="json") for warning in receipt.warnings],
            "source_identity": source_identity(),
            "receipt": receipt.model_dump(mode="json"),
        }
        write_json(output / "verification.json", result)
        print(
            json.dumps(
                {
                    "acceptance": result["acceptance"],
                    "duplicate": result["duplicate_submission"],
                    "provider_retries": result["provider_retries"],
                    "operator_reviews": result["operator_reviews"],
                    "desktop_seconds": result["desktop_seconds"],
                    "fresh_provider_calls": result["fresh_provider_calls"],
                    "reused_provider_responses": result["reused_provider_responses"],
                    "warnings": result["warnings"],
                },
                indent=1,
            )
        )
        if not checks["passed"] or not all(result["duplicate_submission"].values()):
            raise RuntimeError("Fresh official-sample acceptance failed; see verification.json")
    finally:
        service.store.engine.dispose()


if __name__ == "__main__":
    main()
