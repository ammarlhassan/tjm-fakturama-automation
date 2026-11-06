"""Complete the unchanged official sample through its existing journaled job.

The source image and extracted values are never edited. This job's extraction is a manual
transcription of the visible page, recorded as reviewed-live: it is real Fakturama, not a
live model call. A separate live run of the same image is `scripts/run_official_fresh.py`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from decimal import Decimal
from pathlib import Path

from image_to_cash.artifacts import source_identity, write_json
from image_to_cash.models import CompletionReceipt, OrderInput, Stage
from image_to_cash.service import Application, public_job

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_JOB = "ed419e30-f254-433b-ab8d-438ba2cd3c31"
OUTPUT = ROOT / "artifacts/official-sample"
RESOLUTION = (
    "User-approved policy: finish the unchanged official sample and flag its reserved .test "
    "email. Source image and extracted values are unchanged; identity, addresses, items, "
    "totals, dates, payment and the document relationship remain strict."
)


def acceptance(order: OrderInput, receipt: CompletionReceipt, documents: dict) -> dict:
    """The brief's required outcome, checked from receipt and observed documents."""
    observed_order, invoice = documents["order"], documents["invoice"]

    def rows(document):
        return [
            (row["sku"], Decimal(row["quantity"]), Decimal(row["printed_net"]))
            for row in document["items"]
        ]

    expected_rows = [(item.sku, item.quantity, item.printed_net) for item in order.items]
    checks = {
        "reference": receipt.external_reference == "WEB-2026-0714-A17",
        "net_570_00": receipt.totals.net == Decimal("570.00"),
        "vat_108_30": receipt.totals.vat == Decimal("108.30"),
        "gross_678_30": receipt.totals.gross == Decimal("678.30"),
        "bank_transfer": receipt.payment_method == "Bank Transfer",
        "literal_paid": receipt.paid is True and order.payment.status == "PAID",
        "payment_date": receipt.payment_date.isoformat() == "2026-07-18",
        "payment_value": receipt.payment_value == Decimal("678.30"),
        "source_order_open": receipt.source_order_state == "open",
        "order_rows": rows(observed_order) == expected_rows,
        "invoice_rows": rows(invoice) == expected_rows,
        "order_persisted": observed_order["persisted"] is True,
        "invoice_persisted": invoice["persisted"] is True,
        "invoice_links_order": invoice["source_order_number"] == receipt.order_number,
        "saved_relationship": receipt.relationship_evidence.get("mechanism")
        == "saved_fakturama_transaction_group",
        "both_addresses": all(
            document["customer"][role][field] == getattr(getattr(order.customer, role), field)
            for document in (observed_order, invoice)
            for role in ("billing", "delivery")
            for field in ("recipient", "street", "postcode", "city", "country")
        ),
        "email_flagged_not_claimed": receipt.source_field_agreement is False
        and receipt.warnings[0].source_value == "marta.klein@example.test",
    }
    return {"checks": checks, "passed": all(checks.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="integration")
    options = parser.parse_args()
    service = Application()
    try:
        image = ROOT / "fixtures/sample.visible.png"
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        record = service.store.get(OFFICIAL_JOB)
        if record["image_hash"] != digest or record["workspace"] != options.workspace:
            raise RuntimeError("The official job does not reference the unchanged sample image")
        fixture = OrderInput.model_validate_json(
            (ROOT / "fixtures/sample.json").read_text(encoding="utf-8")
        )
        source = OrderInput.model_validate(record["source"])
        if source != fixture:
            raise RuntimeError("The official job's source differs from the visible transcription")
        if record["stage"] != Stage.SUCCEEDED:
            if record["stage"] == Stage.NEEDS_REVIEW and not (record["review"] or {}).get(
                "resolved"
            ):
                service.review(OFFICIAL_JOB, None, RESOLUTION)
            service.queue_resume(OFFICIAL_JOB)
            print(json.dumps({"job_id": OFFICIAL_JOB, "phase": "desktop"}), flush=True)
            service.execute(OFFICIAL_JOB, resume=True)
        record = service.store.get(OFFICIAL_JOB)
        print(
            json.dumps(
                {
                    "stage": record["stage"],
                    "review": record["review"],
                    "receipt": bool(record["receipt"]),
                },
                default=str,
            ),
            flush=True,
        )
        service.report(OFFICIAL_JOB)
        OUTPUT.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            service.settings.runtime / "jobs" / OFFICIAL_JOB, OUTPUT, dirs_exist_ok=True
        )
        write_json(OUTPUT / "journal.json", service.store.events(OFFICIAL_JOB))
        write_json(OUTPUT / "job.json", public_job(record))
        if record["stage"] != Stage.SUCCEEDED:
            raise RuntimeError(record["review"])
        repeated, deduplicated = service.submit(
            image.read_bytes(), options.workspace, mode=record["mode"], execute=False
        )
        repeat_receipt = service.execute(OFFICIAL_JOB)
        receipt = CompletionReceipt.model_validate(record["receipt"])
        documents = json.loads((OUTPUT / "observed-documents.json").read_text(encoding="utf-8"))
        result = {
            "job_id": OFFICIAL_JOB,
            "status": record["stage"],
            "mode": "reviewed transcription + real Fakturama; extraction is not a live model call",
            "official_sample": True,
            "source_changes": [],
            "job_revisions": record["revision"],
            "source_values_changed_by_reviews": False,
            "acceptance": acceptance(source, receipt, documents),
            "duplicate_submission": {
                "same_job": repeated == OFFICIAL_JOB,
                "deduplicated": deduplicated,
                "same_receipt": repeat_receipt == receipt,
            },
            "warnings": [warning.model_dump(mode="json") for warning in receipt.warnings],
            "shared_master_disclosure": (
                "Debtor CUST000001 was an existing test master whose email an earlier "
                "compatibility test changed to .com through the UI. This run did not change it."
            ),
            "source_identity": source_identity(),
            "receipt": receipt.model_dump(mode="json"),
        }
        write_json(OUTPUT / "verification.json", result)
        print(
            json.dumps(
                {"acceptance": result["acceptance"], "duplicate": result["duplicate_submission"]},
                indent=2,
            )
        )
        if not result["acceptance"]["passed"] or not all(result["duplicate_submission"].values()):
            raise RuntimeError("Official-sample acceptance failed; see verification.json")
    finally:
        service.store.engine.dispose()


if __name__ == "__main__":
    main()
