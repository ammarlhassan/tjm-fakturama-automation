"""Observe real Fakturama boundary arithmetic in owned drafts, without saving documents."""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from filelock import FileLock
from generate_fixtures import render

from image_to_cash.adapters import exact_debtor
from image_to_cash.artifacts import ingest, write_json
from image_to_cash.models import OrderInput, Stage
from image_to_cash.service import Application
from image_to_cash.validation import calculate, line_net, verify_document
from image_to_cash.windows import WindowsAdapter

ROOT = Path(__file__).resolve().parents[1]


def probe(service, workspace, name, quantity, price, count, document_discount="0"):
    source = OrderInput.model_validate_json(
        (ROOT / "fixtures/compatibility.json").read_text(encoding="utf-8")
    )
    source.external_reference = "PROBE-" + name.upper()
    if Decimal(document_discount):
        source.document_discount_percent = Decimal(document_discount)
    source.items = [source.items[0].model_copy(deep=True) for _ in range(count)]
    for item in source.items:
        item.quantity, item.unit_net, item.discount_percent = (
            Decimal(quantity),
            Decimal(price),
            Decimal("0"),
        )
        item.printed_net = line_net(item)
    source.totals = calculate(source)
    output = ROOT / "artifacts/rounding" / name
    output.mkdir(parents=True, exist_ok=True)
    image = output / "source.png"
    render(source, image)
    digest, normalized = ingest(image.read_bytes(), service.settings)
    job, _ = service.store.submit(
        workspace, digest, str(normalized), mode="desktop-probe", execute=False
    )
    service.store.update(
        job,
        source=source.model_dump(mode="json"),
        external_reference=source.external_reference,
        stage=Stage.VALIDATED,
    )
    adapter, clean, observed, findings = None, False, None, []
    service.store.own_workspace(workspace, job, "controlled-rounding-probe", desktop=True)

    def action(name, operation, **intent):
        service.store.log(job, Stage.ORDER_OPEN, name, "intent", intent)
        result = operation()
        service.store.log(
            job,
            Stage.ORDER_OPEN,
            name,
            "observed",
            result.model_dump(mode="json")
            if hasattr(result, "model_dump")
            else result
            if isinstance(result, dict)
            else {},
        )
        return result

    try:
        adapter = WindowsAdapter(service.settings, workspace, source)
        action("prepare_probe", adapter.prepare)
        assert not adapter.saved("Order", source.external_reference) and not adapter.saved(
            "Invoice", source.external_reference
        )
        opened = action("open_probe", lambda: adapter.open_order(source))
        contacts = [
            record
            for record in adapter.debtor_candidates(source.customer)
            if exact_debtor(record, source.customer)
        ]
        assert len(contacts) == 1, "Rounding probes require the established compatibility Debtor"
        action("select_probe_debtor", lambda: adapter.select_debtor(contacts[0]))
        for index, item in enumerate(source.items):
            candidates = [
                record
                for record in adapter.product_candidates(item.sku)
                if record["sku"] == item.sku
            ]
            assert len(candidates) == 1
            action(
                f"select_probe_product:{index}",
                lambda record=candidates[0], i=index: adapter.select_product(record, i),
            )
            action(f"set_probe_line:{index}", lambda row=item, i=index: adapter.set_line(i, row))
        observed = adapter.read_order()
        findings = verify_document(source, observed, persisted=False)
        adapter.evidence("observed-rounding", output)

        def discard_owned():
            driver = adapter.driver
            pane = driver._doc()
            assert driver.ui.get(pane, "No.") == opened.number
            assert driver.ui.get(pane, "Cust.Ref.") == source.external_reference
            driver.ui.discard_current_draft("New Order")

        action("discard_owned_probe_draft", lambda: adapter.executor.submit(discard_owned).result())
        assert not adapter.saved("Order", source.external_reference) and not adapter.saved(
            "Invoice", source.external_reference
        )
        clean = True
        service.store.update(
            job,
            stage=Stage.FAILED,
            review={
                "code": "ADAPTER_PROBE_COMPLETED",
                "message": "Measurement-only draft discarded. No Order or Invoice was saved; this is not a document workflow success.",
            },
        )
        result = {
            "case": name,
            "mode": "real desktop arithmetic probe",
            "job_id": job,
            "financial_documents_saved": 0,
            "owned_draft_discarded": True,
            "profile_matches": not findings,
            "expected": source.model_dump(mode="json"),
            "observed": observed.model_dump(mode="json"),
            "findings": [finding.model_dump(mode="json") for finding in findings],
        }
        write_json(output / "result.json", result)
        write_json(output / "journal.json", service.store.events(job))
        print(
            {"case": name, "profile_matches": not findings, "no_documents_saved": True}, flush=True
        )
        return result
    except Exception as exc:
        write_json(
            output / "interruption.json",
            {
                "exception": type(exc).__name__,
                "message": str(exc),
                "evidence": getattr(exc, "evidence", {}),
            },
        )
        raise
    finally:
        if not clean:
            service.store.update(
                job,
                stage=Stage.NEEDS_REVIEW,
                review={
                    "code": "ADAPTER_PROBE_INTERRUPTED",
                    "message": "Reconcile the owned probe draft before releasing this session. Never resume this job as a document workflow.",
                },
            )
        service.store.release_workspace(workspace, job, quarantine=not clean)
        if adapter:
            adapter.close()


def main():
    cases = (
        ("half-cent", "0.1", "0.05", 1),
        ("one-and-half-cent", "0.3", "0.05", 1),
        ("vat-half-cent", "1", "1.50", 1),
        ("vat-group", "1", "0.03", 2),
        ("document-discount", "1", "100", 1, "5"),
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="integration")
    parser.add_argument("--case", choices=[case[0] for case in cases])
    options = parser.parse_args()
    service = Application()
    settings = service.settings
    try:
        with (
            FileLock(str(settings.runtime / "locks/interactive-desktop.lock"), timeout=0),
            FileLock(
                str(settings.runtime / "locks" / f"workspace-{options.workspace}.lock"), timeout=0
            ),
        ):
            if service.store.worker_health()["workspaces"]:
                raise RuntimeError(
                    "Finish or reconcile active desktop work before arithmetic probes"
                )
            rows = [
                probe(service, options.workspace, *case)
                for case in cases
                if options.case is None or case[0] == options.case
            ]
        # Preserve distinct measured cases when running one focused probe.
        if options.case:
            import json

            rows = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted((ROOT / "artifacts/rounding").glob("*/result.json"))
            ]
        write_json(
            ROOT / "artifacts/rounding/results.json",
            {
                "mode": "real desktop, no financial documents saved",
                "cases": rows,
                "all_profile_matches": all(row["profile_matches"] for row in rows),
            },
        )
    finally:
        service.store.engine.dispose()


if __name__ == "__main__":
    main()
