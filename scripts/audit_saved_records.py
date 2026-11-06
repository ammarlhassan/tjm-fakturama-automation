"""Read-only audit of saved masters and documents, captured after a completed run.

Opens saved editors, reads them, screenshots them and closes them. It never saves.
Results are labelled as later audits, not recordings of the original creation steps.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from filelock import FileLock

from image_to_cash.artifacts import source_identity, write_json
from image_to_cash.models import OrderInput, ReviewRequired
from image_to_cash.service import Application
from image_to_cash.validation import documents_row_findings
from image_to_cash.windows import WindowsAdapter, value

ROOT = Path(__file__).resolve().parents[1]


def audit(driver, source: OrderInput, output: Path) -> dict:
    ui = driver.ui
    result: dict = {}
    # Row comparison requires the reopened customer, so read the documents first.
    documents = {}
    for kind in ("Order", "Invoice"):
        saved = driver.saved(kind, source.external_reference)
        if len(saved) != 1:
            raise ReviewRequired("AUDIT_DOCUMENT_COUNT", f"Expected one saved {kind}")
        document = saved[0]
        documents[kind.lower()] = {
            "number": document.number,
            "state": document.state,
            "document_date": document.document_date.isoformat(),
            "gross": format(document.totals.gross, "f"),
            "documents_row": document.documents_row,
            "row_findings": [f.model_dump() for f in documents_row_findings(document)],
        }
    result["documents"] = documents

    pane, rows = driver._rows("Documents", source.external_reference, document_kind="Order")
    ui.settle(pane)
    result["documents_list_evidence"] = driver.evidence("audit-documents-orders", output)
    pane, rows = driver._rows("Documents", source.external_reference, document_kind="Invoice")
    ui.settle(pane)
    result["documents_list_invoice_evidence"] = driver.evidence("audit-documents-invoices", output)

    contact = driver.observe_saved_contact(source.customer, output)
    result["contact"] = contact
    pane = driver._show_contact_role("Delivery address")
    result["contact_delivery"] = {
        "address_type": ui.get(pane, "address type"),
        "additional_name": ui.get(pane, "additional name"),
        "street": ui.get(pane, "Street"),
        "postcode": ui.get(pane, "ZIP - City", index=0),
        "city": ui.get(pane, "ZIP - City", index=1),
        "evidence": driver.evidence("audit-contact-delivery", output),
    }
    ui.tab("Miscellaneous", scope=driver._contact())
    pane = driver._contact()
    ui.settle(pane)
    result["contact_miscellaneous"] = {
        "alias": ui.get(pane, "Alias name"),
        "discount": ui.get(pane, "Discount"),
        "net_or_gross": ui.get(pane, "Net or Gross", role="ComboBox"),
        "payment": ui.get(pane, "Payment", role="ComboBox"),
        "evidence": driver.evidence("audit-contact-miscellaneous", output),
    }
    ui.close_saved_editor()

    method = source.payment.method
    pane, rows = driver._rows("terms of payment", method)
    exact = [i for i, row in enumerate(rows) if value(row, "Name") == method]
    if len(exact) != 1:
        raise ReviewRequired("AUDIT_PAYMENT_COUNT", f"Expected one payment method {method}")
    ui.open_row(pane, exact[0])
    form = ui.wait(driver._payment)
    ui.settle(form)
    result["payment"] = {
        "name": ui.get(form, "Name"),
        "description": ui.get(form, "Description"),
        "code": ui.get(form, "!editorPaymentPaymentcode!", role="ComboBox").strip(),
        "account": ui.get(form, "Account", role="ComboBox"),
        "cash_discount": ui.get(form, "Cash discount"),
        "discount_days": ui.get(form, "Discount Days"),
        "net_days": ui.get(form, "Net Days"),
        "evidence": driver.evidence("audit-payment", output),
    }
    ui.close_saved_editor()

    result["vats"], result["products"] = [], []
    for rate in sorted({item.vat_percent for item in source.items}):
        name = f"VAT {format(rate.normalize(), 'f')}%"
        pane, rows = driver._rows("VATs", name)
        exact = [i for i, row in enumerate(rows) if value(row, "Name") == name]
        if len(exact) != 1:
            raise ReviewRequired("AUDIT_VAT_COUNT", f"Expected one VAT definition {name}")
        ui.open_row(pane, exact[0])
        form = ui.wait(driver._vat)
        ui.settle(form)
        labels = [
            c.window_text()
            for c in ui.controls(form, "ComboBox")
            if "code" in c.window_text().casefold()
        ]
        result["vats"].append(
            {
                "name": ui.get(form, "Name"),
                "value": ui.get(form, "Value"),
                "code": ui.get(form, labels[0], role="ComboBox").strip(),
                "evidence": driver.evidence(f"audit-vat-{format(rate.normalize(), 'f')}", output),
            }
        )
        ui.close_saved_editor()

    for sku in dict.fromkeys(item.sku for item in source.items):
        pane, rows = driver._rows("Products", sku)
        exact = [i for i, row in enumerate(rows) if value(row, "Item Number", "Item No.") == sku]
        if len(exact) != 1:
            raise ReviewRequired("AUDIT_PRODUCT_COUNT", f"Expected one Product {sku}")
        ui.open_row(pane, exact[0])
        form = ui.wait(driver._product)
        ui.settle(form)
        result["products"].append(
            {
                "item_number": ui.get(form, "Item Number"),
                "name": ui.get(form, "Name"),
                "description": ui.get(form, "Description"),
                "price_gross": ui.get(form, "Price (gross)"),
                "cost_price_net": ui.get(form, "cost price (net)"),
                "vat": ui.get(form, "VAT", role="ComboBox"),
                "stock": ui.get(form, "Stock"),
                "evidence": driver.evidence(f"audit-product-{sku}", output),
            }
        )
        ui.close_saved_editor()
    ui.restore_parts()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--audits", required=True, help="The completed run this audit inspects")
    options = parser.parse_args()
    service = Application()
    settings = service.settings
    source = OrderInput.model_validate_json(options.source.read_text(encoding="utf-8"))
    output = options.output if options.output.is_absolute() else ROOT / options.output
    output.mkdir(parents=True, exist_ok=True)
    try:
        locks = settings.runtime / "locks"
        with (
            FileLock(str(locks / "interactive-desktop.lock"), timeout=0),
            FileLock(str(locks / f"workspace-{options.workspace}.lock"), timeout=0),
        ):
            if service.store.worker_health()["workspaces"]:
                raise RuntimeError("Reconcile active desktop work before auditing saved records")
            adapter = WindowsAdapter(settings, options.workspace, source)
            try:
                adapter.executor.submit(adapter.driver.ui.activate).result()
                adapter.executor.submit(adapter.driver.ui.close_clean_editors).result()
                result = adapter.executor.submit(audit, adapter.driver, source, output).result()
            finally:
                adapter.close()
        result = {
            "mode": "later read-only audit of saved records; not a recording of creation",
            "audits": options.audits,
            "workspace": options.workspace,
            "financial_documents_modified": False,
            "source_identity": source_identity(),
            **result,
        }
        write_json(output / "result.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    finally:
        service.store.engine.dispose()


if __name__ == "__main__":
    main()
