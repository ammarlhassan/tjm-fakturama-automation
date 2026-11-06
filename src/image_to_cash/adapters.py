"""Semantic desktop port and a persistent, explicitly simulated test implementation."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from .artifacts import write_json
from .models import Customer, ItemInput, ObservedDocument, OrderInput, ReviewRequired
from .validation import master_gross, normalize, text_equal


class DesktopAdapter(Protocol):
    mode: str

    def prepare(self) -> dict: ...
    def saved(self, kind: str, reference: str) -> list[ObservedDocument]: ...
    def current_order(self, reference: str) -> ObservedDocument | None: ...
    def open_order(self, source: OrderInput) -> ObservedDocument: ...
    def debtor_candidates(self, source: Customer) -> list[dict]: ...
    def begin_debtor(self, source: Customer) -> None: ...
    def create_debtor(self, source: Customer, payment_method: str) -> dict: ...
    def select_debtor(self, record: dict) -> None: ...
    def payment_candidates(self, name: str) -> list[dict]: ...
    def create_payment(self, name: str, code: str) -> dict: ...
    def vat_candidates(self, name: str) -> list[dict]: ...
    def create_vat(self, name: str, rate: Decimal) -> dict: ...
    def product_candidates(self, sku: str) -> list[dict]: ...
    def create_product(self, item: ItemInput) -> dict: ...
    def select_product(self, record: dict, index: int) -> None: ...
    def set_line(self, index: int, item: ItemInput) -> None: ...
    def read_order(self) -> ObservedDocument: ...
    def save_order(self) -> None: ...
    def current_invoice(self, order_number: str) -> ObservedDocument | None: ...
    def follow_up_invoice(self, order: ObservedDocument) -> ObservedDocument: ...
    def set_payment(self, source: OrderInput) -> None: ...
    def read_invoice(self) -> ObservedDocument: ...
    def save_invoice(self) -> None: ...
    def relationship(self, order_number: str, invoice_number: str, reference: str) -> dict: ...
    def evidence(self, label: str, destination: Path) -> list[str]: ...
    def discard_order_draft(self, number: str, reference: str) -> dict: ...
    def observe_saved_contact(self, customer: Customer, destination: Path) -> dict: ...


class InjectedCrash(BaseException):
    """Used only by controlled recovery demonstrations/tests after an action's effect."""


class SimulatedAdapter:
    mode = "simulation"

    def __init__(self, path: Path, *, crash_at: str | None = None):
        self.path = path
        self.crash_at = crash_at
        self.state = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {
                "debtors": [],
                "payments": [],
                "vats": [],
                "products": [],
                "documents": [],
                "order": None,
                "invoice": None,
                "counter": 0,
                "standard_vat": "unchanged",
                "standard_payment": "unchanged",
                "calls": [],
            }
        )

    def _persist(self, action: str):
        self.state["calls"].append(action)
        write_json(self.path, self.state)
        if self.crash_at == action:
            self.crash_at = None
            raise InjectedCrash(action)

    def prepare(self):
        return {"mode": "simulation", "read_only_product_search": True}

    def _next(self, prefix: str) -> str:
        self.state["counter"] += 1
        return f"SIM-{prefix}-{self.state['counter']:05d}"

    def saved(self, kind, reference):
        return [
            ObservedDocument.model_validate(d)
            for d in self.state["documents"]
            if d["kind"] == kind and d["external_reference"] == reference
        ]

    def current_order(self, reference):
        value = self.state["order"]
        return (
            ObservedDocument.model_validate(value)
            if value and value["external_reference"] == reference
            else None
        )

    def open_order(self, source):
        if self.state["order"] and not self.state["order"]["persisted"]:
            raise ReviewRequired("UNRELATED_EDITOR", "A simulated unsaved Order already exists")
        document = ObservedDocument(
            number=self._next("ORDER"),
            kind="Order",
            external_reference=source.external_reference or "",
            currency=source.currency,
            order_date=source.order_date or date.today(),
            document_date=source.order_date or date.today(),
            service_date=date.today(),
            customer=Customer(),
            items=[],
            totals=source.totals,
            document_discount_percent=source.document_discount_percent or Decimal("0"),
            shipping_net=source.shipping_net or Decimal("0"),
        )
        self.state["order"] = document.model_dump(mode="json")
        self._persist("open_order")
        return document

    def debtor_candidates(self, source):
        if not self.state["order"]:
            raise RuntimeError("Debtor checks require an open Order selector")
        self._persist("debtor_selector")
        return [
            d
            for d in self.state["debtors"]
            if text_equal(d["customer"].get("company"), source.company)
            and text_equal(d["customer"].get("last_name"), source.last_name)
        ]

    def create_debtor(self, source, payment_method):
        record = {
            "id": self._next("CUSTOMER"),
            "customer": source.model_dump(mode="json"),
            "payment_method": payment_method,
            "discount": "0",
            "pricing": "Net",
            "salutation": "---",
        }
        self.state["debtors"].append(record)
        self._persist("create_debtor")
        return record

    def begin_debtor(self, source):
        self._persist("begin_debtor")

    def select_debtor(self, record):
        self.state["order"]["customer"] = deepcopy(record["customer"])
        self._persist("select_debtor")

    def payment_candidates(self, name):
        return [p for p in self.state["payments"] if p["name"] == name]

    def create_payment(self, name, code):
        record = {
            "id": self._next("PAYMENT"),
            "name": name,
            "description": name,
            "code": code,
            "account": "",
            "cash_discount": "0",
            "discount_days": 0,
            "net_days": 0,
            "unpaid_text": "",
            "deposit_text": "",
            "paid_text": "",
            "standard": False,
        }
        self.state["payments"].append(record)
        self._persist("create_payment")
        return record

    def vat_candidates(self, name):
        return [v for v in self.state["vats"] if v["name"] == name]

    def create_vat(self, name, rate):
        record = {
            "id": self._next("VAT"),
            "name": name,
            "description": name,
            "rate": str(rate),
            "code": "S (Standard rate)",
        }
        self.state["vats"].append(record)
        self._persist("create_vat")
        return record

    def product_candidates(self, sku):
        if not self.state["order"]:
            raise RuntimeError("Product checks require the Order selector")
        self._persist(f"product_selector:{sku}")
        return [p for p in self.state["products"] if p["sku"] == sku]

    def create_product(self, item):
        record = {
            "id": self._next("PRODUCT"),
            "sku": item.sku,
            "name": item.description,
            "description": item.description,
            "gross": str(master_gross(item)),
            "vat_percent": str(item.vat_percent),
            "cost_net": "0",
            "stock": "0",
        }
        self.state["products"].append(record)
        self._persist("create_product")
        return record

    def select_product(self, record, index):
        lines = self.state["order"]["items"]
        if index > len(lines):
            raise RuntimeError("Rows must be selected sequentially")
        if index == len(lines):
            lines.append(
                ItemInput(
                    sku=record["sku"],
                    description=record.get("description"),
                    quantity=Decimal("1"),
                    unit_net=Decimal("0"),
                    discount_percent=Decimal("0"),
                    vat_percent=Decimal(str(record["vat_percent"])),
                    printed_net=Decimal("0"),
                ).model_dump(mode="json")
            )
        elif lines[index]["sku"] != record["sku"]:
            raise ReviewRequired("ROW_IDENTITY_CONFLICT", "Surviving row has a different SKU")
        self._persist(f"select_product:{index}")

    def set_line(self, index, item):
        lines = self.state["order"]["items"]
        if index > len(lines):
            raise RuntimeError("Rows must be completed sequentially")
        if index == len(lines):
            lines.append(item.model_dump(mode="json"))
        else:
            lines[index] = item.model_dump(mode="json")
        self._persist(f"set_line:{index}")

    def read_order(self):
        return ObservedDocument.model_validate(self.state["order"])

    def discard_order_draft(self, number, reference):
        draft = self.state["order"]
        if not draft or draft["persisted"]:
            return {"number": number, "external_reference": reference, "draft_present": False}
        if draft["number"] != number or draft["external_reference"] != reference:
            raise ReviewRequired(
                "DRAFT_IDENTITY_UNCONFIRMED", "The open simulated draft belongs to another source"
            )
        self.state["order"] = None
        self._persist("discard_order_draft")
        return {"number": number, "external_reference": reference, "draft_present": True}

    def save_order(self):
        self.state["order"]["persisted"] = True
        self.state["documents"] = [
            d for d in self.state["documents"] if d["number"] != self.state["order"]["number"]
        ]
        self.state["documents"].append(deepcopy(self.state["order"]))
        self._persist("save_order")

    def current_invoice(self, order_number):
        value = self.state["invoice"]
        return (
            ObservedDocument.model_validate(value)
            if value and value["source_order_number"] == order_number
            else None
        )

    def follow_up_invoice(self, order):
        if not order.persisted:
            raise RuntimeError("Follow-up requires a verified saved Order")
        invoice = order.model_copy(deep=True)
        invoice.number, invoice.kind = self._next("INVOICE"), "Invoice"
        invoice.document_date = invoice.service_date = date.today()
        invoice.source_order_number, invoice.persisted = order.number, False
        self.state["invoice"] = invoice.model_dump(mode="json")
        self._persist("follow_up_invoice")
        return invoice

    def set_payment(self, source):
        if len(self.payment_candidates(source.payment.method)) != 1:
            raise ReviewRequired(
                "PAYMENT_UNAVAILABLE", "Exact Invoice payment method is unavailable"
            )
        paid = normalize(source.payment.status or "").upper() == "PAID"
        self.state["invoice"].update(
            payment_method=source.payment.method,
            paid=paid,
            payment_date=source.payment.payment_date.isoformat()
            if paid and source.payment.payment_date
            else None,
            payment_value=str(source.totals.gross) if paid else None,
            state="paid" if paid else "open",
        )
        self._persist("set_payment")

    def read_invoice(self):
        return ObservedDocument.model_validate(self.state["invoice"])

    def save_invoice(self):
        self.state["invoice"]["persisted"] = True
        self.state["documents"] = [
            d for d in self.state["documents"] if d["number"] != self.state["invoice"]["number"]
        ]
        self.state["documents"].append(deepcopy(self.state["invoice"]))
        self._persist("save_invoice")

    def relationship(self, order_number, invoice_number, reference):
        orders, invoices = self.saved("Order", reference), self.saved("Invoice", reference)
        if (
            len(orders) != 1
            or len(invoices) != 1
            or orders[0].number != order_number
            or invoices[0].number != invoice_number
            or invoices[0].source_order_number != order_number
        ):
            raise ReviewRequired("INVOICE_LINK_MISMATCH", "Simulated saved parent relation differs")
        return {
            "mechanism": "simulation_saved_parent",
            "order_number": order_number,
            "invoice_number": invoice_number,
        }

    def observe_saved_contact(self, customer, destination):
        matches = [record for record in self.state["debtors"] if exact_debtor(record, customer)]
        if len(matches) != 1:
            raise ReviewRequired(
                "SAVED_CUSTOMER_AMBIGUOUS", "Cannot uniquely reopen the simulated Debtor"
            )
        saved = Customer.model_validate(matches[0]["customer"])
        return {
            "mechanism": "simulation_saved_contact",
            "customer_id": matches[0]["id"],
            "email": saved.email,
            "telephone": saved.telephone,
            "evidence": self.evidence("saved-contact", destination),
        }

    def evidence(self, label, destination):
        path = destination / f"{label}-SIMULATED.json"
        write_json(
            path,
            {
                "mode": "simulation",
                "warning": "This is a test-double observation, not Fakturama evidence",
                "state": self.state,
            },
        )
        return [path.name]


def exact_debtor(record: dict, source: Customer) -> bool:
    customer = Customer.model_validate(record["customer"])
    return all(
        text_equal(getattr(customer, f), getattr(source, f))
        for f in ("company", "first_name", "last_name")
    ) and all(
        text_equal(getattr(customer.billing, f), getattr(source.billing, f))
        for f in ("postcode", "city")
    )


def same_debtor_identity(record: dict, source: Customer) -> bool:
    """Detect a possible address conflict, not merely a fuzzy search result."""
    customer = Customer.model_validate(record["customer"])
    return all(
        text_equal(getattr(customer, field), getattr(source, field))
        for field in ("company", "first_name", "last_name")
        if getattr(source, field)
    )
