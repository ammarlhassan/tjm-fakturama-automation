"""Controlled real-desktop acceptance; requires the initialized test workspace.

Faults stop orchestration after a real UI action returns, before its observed
journal entry. They do not kill Fakturama or alter its internal data.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from decimal import Decimal
from pathlib import Path

from acceptance_common import (
    RETRY_REASON,
    awaiting_provider_retry,
    extract_bounded,
    review_counts,
)
from generate_fixtures import render

from image_to_cash.adapters import InjectedCrash
from image_to_cash.artifacts import source_identity, write_json
from image_to_cash.evaluation import differences
from image_to_cash.models import OrderInput, Stage
from image_to_cash.service import Application, public_job
from image_to_cash.validation import calculate
from image_to_cash.windows import WindowsAdapter
from image_to_cash.workflow import Workflow

ROOT = Path(__file__).resolve().parents[1]


class StopAfterAction:
    mode = "live"

    def __init__(self, inner, action, evidence):
        self.inner, self.action, self.destination = inner, action, evidence

    def __getattr__(self, name):
        operation = getattr(self.inner, name)

        def invoke(*args, **kwargs):
            result = operation(*args, **kwargs)
            if name == self.action:
                self.inner.evidence(f"interrupted-after-{name}", self.destination)
                raise InjectedCrash(name)
            return result

        return invoke


def run_case(service, workspace, scenario, series=None, explicit_source=False):
    mode = "reviewed-live" if explicit_source else "live"
    order = OrderInput.model_validate_json(
        (ROOT / "fixtures/compatibility.json").read_text(encoding="utf-8")
    )
    order.external_reference = {
        "clean": "CLEAN-WIN-2026-C17",
        "order-save": "RECOVER-ORDER-2026-B17",
        "invoice-draft": "RECOVER-INVOICE-2026-B17",
        "fresh-masters": "FRESH-MASTERS-2026-C17",
        "unpaid": "UNPAID-WIN-2026-C17",
    }[scenario]
    if series:
        order.external_reference += f"-{series}"
    if scenario == "fresh-masters":
        tag = series or "C"
        order.customer.company = f"Acceptance Fresh Office {tag} GmbH"
        order.customer.first_name, order.customer.last_name = "Ada", "Weber"
        order.customer.alias, order.customer.email = (
            f"ACCEPT-FRESH-{tag}",
            f"fresh-{tag.lower()}@example.com",
        )
        order.customer.telephone = "+493055501420"
        order.customer.billing.recipient = order.customer.company
        order.customer.billing.street = "Altmarkt 1"
        order.customer.billing.postcode, order.customer.billing.city = "01067", "Dresden"
        order.customer.delivery.recipient = "Acceptance Fresh Warehouse"
        order.payment.method = "SEPA Direct Debit"
        order.items[0].sku, order.items[1].sku = f"FRESH-{tag}-CHAIR", f"FRESH-{tag}-MAT"
        order.items[1].vat_percent = Decimal("9")
        order.document_discount_percent = Decimal("5")
        order.totals = calculate(order)
    if scenario == "unpaid":
        order.payment.status, order.payment.payment_date = "UNPAID", None
    output = (
        ROOT / "artifacts/final-acceptance" / series / scenario
        if series
        else ROOT
        / "artifacts"
        / {
            "clean": "desktop-clean",
            "order-save": "desktop-recovery",
            "invoice-draft": "desktop-invoice-recovery",
            "fresh-masters": "desktop-fresh-masters",
            "unpaid": "desktop-unpaid",
        }[scenario]
    )
    inputs = output if series else ROOT / "fixtures/acceptance"
    inputs.mkdir(parents=True, exist_ok=True)
    stem = "input" if series else scenario
    image = inputs / f"{stem}.png"
    render(order, image)
    write_json(
        inputs / ("expected.json" if series else f"{scenario}.json"), order.model_dump(mode="json")
    )
    job_id, duplicate = service.submit(image.read_bytes(), workspace, mode=mode, execute=False)
    print(
        json.dumps(
            {"scenario": scenario, "job_id": job_id, "duplicate": duplicate, "phase": "extracting"}
        ),
        flush=True,
    )
    started = time.perf_counter()
    record = service.store.get(job_id)
    interrupted = False
    desktop_seconds = None
    if record["stage"] != Stage.SUCCEEDED:
        if awaiting_provider_retry(record):
            service.review(job_id, None, RETRY_REASON)
            service.queue_resume(job_id)
            record = service.store.get(job_id)
        if record["stage"] not in {Stage.RECEIVED, Stage.VALIDATED}:
            raise RuntimeError(
                f"Existing case requires explicit reconciliation: {record['review']}"
            )
        if explicit_source:
            # These scenarios test the desktop, so a provider outage need not block them.
            # The report labels this input as not coming from a live model response.
            service.store.update(
                job_id,
                source=order.model_dump(mode="json"),
                external_reference=order.external_reference,
                stage=Stage.VALIDATED,
                extraction={"provenance": "explicit synthetic acceptance source"},
            )
            service.store.log(
                job_id,
                Stage.VALIDATED,
                "explicit_acceptance_source",
                "observed",
                {"fixture": str(inputs / "expected.json"), "live_extraction": False},
            )
        else:
            record = extract_bounded(service, job_id, attempts=6, wait_seconds=150)
            if record["stage"] != Stage.VALIDATED:
                raise RuntimeError(record["review"])
            if differences(order, OrderInput.model_validate(record["source"])):
                raise RuntimeError("Live extraction differs from independently specified fixture")
        desktop_started = time.perf_counter()
        adapter = WindowsAdapter(service.settings, workspace, order)
        point = {
            "clean": None,
            "order-save": "save_order",
            "invoice-draft": "set_payment",
            "fresh-masters": None,
            "unpaid": None,
        }[scenario]
        try:
            wrapped = (
                StopAfterAction(adapter, point, service.settings.runtime / "jobs" / job_id)
                if point
                else adapter
            )
            try:
                Workflow(service.settings, service.store, wrapped).run(job_id)
            except InjectedCrash:
                interrupted = True
                service.store.log(
                    job_id,
                    Stage.RECONCILING,
                    "injected_orchestrator_interruption",
                    "observed",
                    {
                        "after": point,
                        "desktop": "real Fakturama",
                        "application_process_killed": False,
                    },
                )
                service.report(job_id)
        finally:
            adapter.close()
        if point:
            if not interrupted:
                raise RuntimeError(
                    f"The controlled interruption was not reached: {service.store.get(job_id)['review']}"
                )
            service.queue_resume(job_id)
            service.execute(job_id, resume=True)
        desktop_seconds = time.perf_counter() - desktop_started
    record = service.store.get(job_id)
    if record["stage"] != Stage.SUCCEEDED:
        raise RuntimeError(record["review"])
    repeated, deduplicated = service.submit(image.read_bytes(), workspace, mode=mode, execute=False)
    if repeated != job_id or not deduplicated:
        raise RuntimeError("Duplicate submission did not return the original completed job")
    service.report(job_id)
    shutil.copytree(service.settings.runtime / "jobs" / job_id, output, dirs_exist_ok=True)
    events = service.store.events(job_id)
    write_json(output / "journal.json", events)
    write_json(output / "job.json", public_job(record))
    previous = output / "verification.json"
    if duplicate and previous.exists():
        original = json.loads(previous.read_text(encoding="utf-8"))
        if original.get("job_id") == job_id:
            print(
                json.dumps(
                    {
                        "scenario": scenario,
                        "stage": record["stage"],
                        "duplicate_verified": True,
                        "original_measurement_retained": True,
                    }
                ),
                flush=True,
            )
            return original
    interrupted = interrupted or any(
        event["action"] == "injected_orchestrator_interruption"
        for event in service.store.events(job_id)
    )
    result = {
        "scenario": scenario,
        "job_id": job_id,
        "mode": "explicit synthetic source and real desktop; no live model call"
        if explicit_source
        else "live vision and real desktop",
        "official_sample": False,
        "source_changes": [
            "synthetic customer, addresses, email and alias",
            "SEPA Direct Debit method",
            "new SKUs with 19% and 9% VAT",
            "5% document discount",
            "leading-zero postcode",
            "distinct reference",
        ]
        if scenario == "fresh-masters"
        else [".com email", "distinct acceptance reference", "UNPAID status with no payment date"]
        if scenario == "unpaid"
        else [".com email", "distinct acceptance reference"],
        "status": record["stage"],
        "series": series,
        "job_revisions": record["revision"],
        **review_counts(events),
        "controlled_interruption": interrupted,
        "duplicate_submission_verified": True,
        "seconds_including_controlled_resume": time.perf_counter() - started
        if not duplicate
        else None,
        "desktop_seconds": desktop_seconds,
        "source_identity": source_identity(),
        "receipt": record["receipt"],
    }
    write_json(output / "verification.json", result)
    print(
        json.dumps(
            {"scenario": scenario, "stage": record["stage"], "report": str(output / "report.html")}
        ),
        flush=True,
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="integration")
    parser.add_argument(
        "--scenario",
        choices=["all", "clean", "order-save", "invoice-draft", "fresh-masters", "unpaid"],
        default="all",
    )
    parser.add_argument(
        "--series",
        help="Run new references and write to artifacts/final-acceptance/SERIES, leaving earlier evidence unchanged",
    )
    parser.add_argument(
        "--explicit-source",
        action="store_true",
        help="Use the specified source instead of a live model call, labelled reviewed-live",
    )
    options = parser.parse_args()
    if options.series and (not options.series.isalnum() or len(options.series) > 8):
        parser.error("series must contain 1-8 letters or digits")
    service = Application()
    try:
        cases = (
            ("clean", "order-save", "invoice-draft", "unpaid", "fresh-masters")
            if options.scenario == "all"
            else (options.scenario,)
        )
        for scenario in cases:
            run_case(service, options.workspace, scenario, options.series, options.explicit_source)
    finally:
        service.store.engine.dispose()


if __name__ == "__main__":
    main()
