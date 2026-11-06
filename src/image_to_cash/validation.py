"""Business checks never depend on model confidence or desktop coordinates."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .models import (
    ExtractionResult,
    Finding,
    ItemInput,
    ObservedDocument,
    OrderInput,
    SourceValueWarning,
    Totals,
    ValidationReport,
)

CENT = Decimal("0.01")
HUNDRED = Decimal("100")
PROFILE = "eur-net-vat-group-half-up-v1"
DECIMAL_FIELDS = frozenset(
    {
        "quantity",
        "unit_net",
        "discount_percent",
        "vat_percent",
        "printed_net",
        "document_discount_percent",
        "shipping_net",
        "shipping_vat_percent",
        "net",
        "vat",
        "gross",
    }
)
PAYMENT_CODES = {
    "Bank Transfer": "Credit transfer",
    "Credit Card": "Credit card",
    "SEPA Direct Debit": "SEPA direct debit",
}


def text_equal(left: str | None, right: str | None) -> bool:
    return normalize(left or "") == normalize(right or "")


def reserved_email_domain(email: str | None) -> bool:
    """RFC 6761 reserves .test; Fakturama 2.2.0's validator will not assign such an address."""
    parts = normalize(email or "").casefold().rsplit("@", 1)
    return len(parts) == 2 and parts[0] != "" and parts[1].endswith(".test")


def email_exception_applies(source_email: str | None, policy: str) -> bool:
    return policy == "flag-reserved-domain" and reserved_email_domain(source_email)


def email_warning(
    source_email: str | None, persisted_email: str | None, policy: str
) -> SourceValueWarning | None:
    """Describe a reserved source email the application did not persist; never rewrite it."""
    if not email_exception_applies(source_email, policy) or text_equal(
        source_email, persisted_email
    ):
        return None
    persisted = normalize(persisted_email or "") or None
    state = "different_existing_value" if persisted else "blank"
    return SourceValueWarning(
        code="SOURCE_EMAIL_NOT_PERSISTED",
        field="customer.email",
        source_value=source_email,
        persisted_value=persisted,
        persisted_state=state,
        policy=policy,
        message=(
            f"The source email {source_email} uses the reserved .test domain and was not "
            + (
                f"persisted; the saved contact holds a different existing value, {persisted}."
                if persisted
                else "persisted; the saved contact's email is blank."
            )
            + " The source is unchanged. This run does not establish complete source-field agreement."
        ),
    )


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def normalized_source(source: dict) -> dict:
    """Canonical business content, retaining identifier spelling and row order."""
    values = OrderInput.model_validate(source).model_dump(mode="json")

    def visit(value, name=""):
        if isinstance(value, dict):
            return {key: visit(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item) for item in value]
        if value is None and name in {"document_discount_percent", "shipping_net"}:
            return "0"
        if isinstance(value, str):
            if name in DECIMAL_FIELDS:
                plain = format(Decimal(value), "f")
                plain = plain.rstrip("0").rstrip(".") if "." in plain else plain
                return "0" if plain == "-0" else plain
            return normalize(value)
        return value

    return visit(values)


def evidence_findings(reading: ExtractionResult, engine: str) -> list[dict]:
    """Require a literal evidence reference for every non-empty transcribed value."""
    available = {item.field for item in reading.evidence if item.raw_text.strip()}
    findings = []
    for path, value in flatten(reading.order).items():
        if value is None:
            continue
        groups = {path}
        if path in {"customer.first_name", "customer.last_name"}:
            groups.add("customer.contact")
        if path.startswith(("customer.billing.", "customer.delivery.")):
            groups.add(path.rsplit(".", 1)[0])
        if re.fullmatch(r"items\.\d+\.[a-z_]+", path):
            groups.add(path.rsplit(".", 1)[0])
        if not groups.intersection(available):
            findings.append({"code": "FIELD_EVIDENCE_MISSING", "field": path, "engine": engine})
    return findings


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def line_net(item: ItemInput) -> Decimal:
    if item.quantity is None or item.unit_net is None or item.discount_percent is None:
        raise ValueError("Incomplete item arithmetic")
    return money(item.quantity * item.unit_net * (1 - item.discount_percent / HUNDRED))


def master_gross(item: ItemInput) -> Decimal:
    if item.unit_net is None or item.vat_percent is None:
        raise ValueError("Missing Product pricing or VAT")
    return money(item.unit_net * (1 + item.vat_percent / HUNDRED))


def calculate(order: OrderInput) -> Totals:
    buckets: dict[Decimal, Decimal] = defaultdict(lambda: Decimal("0"))
    for item in order.items:
        if item.vat_percent is None:
            raise ValueError("Missing VAT")
        buckets[item.vat_percent] += line_net(item)
    discount = order.document_discount_percent or Decimal("0")
    buckets = {rate: money(net * (1 - discount / HUNDRED)) for rate, net in buckets.items()}
    shipping = order.shipping_net or Decimal("0")
    if shipping:
        if order.shipping_vat_percent is None:
            raise ValueError("Shipping VAT must be explicit")
        rate = order.shipping_vat_percent
        buckets[rate] = buckets.get(rate, Decimal("0")) + shipping
    net = money(sum(buckets.values(), Decimal("0")))
    vat = sum((money(net * rate / HUNDRED) for rate, net in buckets.items()), Decimal("0"))
    return Totals(net=net, vat=vat, gross=net + vat)


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            result.update(flatten(item, f"{prefix}.{key}" if prefix else key))
        return result
    if isinstance(value, list):
        result = {}
        for index, item in enumerate(value):
            result.update(flatten(item, f"{prefix}.{index}"))
        return result
    return {prefix: value}


def validate_order(order: OrderInput) -> ValidationReport:
    findings: list[Finding] = []

    def issue(code: str, field: str, message: str, expected=None, observed=None) -> None:
        findings.append(
            Finding(code=code, field=field, message=message, expected=expected, observed=observed)
        )

    required = {
        "order_date": order.order_date,
        "external_reference": order.external_reference,
        "currency": order.currency,
        "payment.method": order.payment.method,
        "payment.status": order.payment.status,
        "totals.net": order.totals.net,
        "totals.vat": order.totals.vat,
        "totals.gross": order.totals.gross,
    }
    if not order.customer.company and not order.customer.last_name:
        required["customer.company"] = None
    for address_name in ("billing", "delivery"):
        address = getattr(order.customer, address_name)
        for field in ("recipient", "street", "postcode", "city", "country"):
            required[f"customer.{address_name}.{field}"] = getattr(address, field)
    for path, value in required.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            issue("MISSING_FIELD", path, "Required visible source value is missing")
    if order.currency and order.currency != "EUR":
        issue("UNSUPPORTED_CURRENCY", "currency", "Initial validated workspace profile is EUR")
    status = normalize(order.payment.status or "").upper()
    if status == "PAID" and order.payment.payment_date is None:
        issue(
            "PAYMENT_DATE_MISSING", "payment.payment_date", "Explicit PAID requires its source date"
        )
    if status not in {"PAID", "UNPAID", "NOT PAID", "PENDING"}:
        issue(
            "PAYMENT_STATUS_AMBIGUOUS", "payment.status", "Status must be explicitly paid or unpaid"
        )
    if order.payment.method and order.payment.method not in PAYMENT_CODES:
        issue("UNSUPPORTED_PAYMENT", "payment.method", "No verified payment-code mapping")
    if not order.items:
        issue("NO_ITEMS", "items", "At least one source item is required")
    complete = True
    for index, item in enumerate(order.items):
        for field in (
            "sku",
            "description",
            "quantity",
            "unit",
            "unit_net",
            "discount_percent",
            "vat_percent",
            "printed_net",
        ):
            value = getattr(item, field)
            if value is None or value == "":
                complete = False
                issue(
                    "MISSING_FIELD", f"items.{index}.{field}", "Every source row must be complete"
                )
        for field in ("unit_net", "printed_net", "vat_percent"):
            value = getattr(item, field)
            if value is not None and value < 0:
                issue(
                    "NEGATIVE_VALUE",
                    f"items.{index}.{field}",
                    "Negative values require a different document workflow",
                )
        if item.quantity is not None and item.quantity <= 0:
            issue("INVALID_QUANTITY", f"items.{index}.quantity", "Quantity must be positive")
        if item.discount_percent is not None and not 0 <= item.discount_percent <= 100:
            issue(
                "INVALID_DISCOUNT",
                f"items.{index}.discount_percent",
                "Discount must be between 0 and 100",
            )
        if all(
            getattr(item, field) is not None
            for field in ("quantity", "unit_net", "discount_percent", "printed_net")
        ):
            expected = line_net(item)
            if expected != item.printed_net:
                issue(
                    "LINE_TOTAL_MISMATCH",
                    f"items.{index}.printed_net",
                    "Printed line total does not reconcile",
                    str(expected),
                    str(item.printed_net),
                )
    discount = order.document_discount_percent or Decimal("0")
    if not 0 <= discount <= 100:
        issue(
            "INVALID_DISCOUNT",
            "document_discount_percent",
            "Document discount must be between 0 and 100",
        )
    if order.shipping_net is not None and order.shipping_net < 0:
        issue("NEGATIVE_VALUE", "shipping_net", "Shipping must not be negative")
    if order.shipping_net and order.shipping_vat_percent is None:
        issue("SHIPPING_VAT_MISSING", "shipping_vat_percent", "Do not infer shipping VAT")
        complete = False
    computed = calculate(order) if complete and order.items else None
    if computed:
        for field in ("net", "vat", "gross"):
            expected, observed = getattr(computed, field), getattr(order.totals, field)
            if observed is not None and expected != observed:
                issue(
                    "TOTAL_MISMATCH",
                    f"totals.{field}",
                    "Source and calculated totals differ",
                    str(expected),
                    str(observed),
                )
    return ValidationReport(findings=findings, computed=computed, profile=PROFILE)


def documents_row_date(text: str) -> date | None:
    """Clipboard rows expose Java's Date text, for example 'Tue Jul 14 00:00:00 EEST 2026'."""
    match = re.fullmatch(r"[A-Z][a-z]{2} ([A-Z][a-z]{2}) (\d{1,2}) \S+ \S+ (\d{4})", text.strip())
    if not match:
        return None
    try:
        return datetime.strptime(" ".join(match.groups()), "%b %d %Y").date()
    except ValueError:
        return None


def documents_row_findings(observed: ObservedDocument) -> list[Finding]:
    """Compare the persisted document with its own row in Data > Documents."""
    row = observed.documents_row
    if not row:
        return []
    findings: list[Finding] = []

    def differs(field: str, expected: Any, seen: Any) -> None:
        findings.append(
            Finding(
                code="DOCUMENTS_ROW_MISMATCH",
                field=f"documents_row.{field}",
                message="The Data > Documents row differs from the saved document",
                expected=str(expected),
                observed=str(seen),
            )
        )

    if row.get("document") != observed.number:
        differs("document", observed.number, row.get("document"))
    if not text_equal(row.get("custref"), observed.external_reference):
        differs("custref", observed.external_reference, row.get("custref"))
    if documents_row_date(row.get("date", "")) != observed.document_date:
        differs("date", observed.document_date, row.get("date"))
    try:
        total = Decimal(row.get("total", "").replace(",", "."))
    except InvalidOperation:
        total = None
    if total is None or total != observed.totals.gross:
        differs("total", observed.totals.gross, row.get("total"))
    return findings


def verify_document(
    source: OrderInput, observed: ObservedDocument, *, persisted: bool, email_policy: str = "strict"
) -> list[Finding]:
    findings: list[Finding] = documents_row_findings(observed)
    expected: dict[str, Any] = {
        "external_reference": source.external_reference,
        "currency": source.currency,
        "order_date": source.order_date.isoformat() if source.order_date else None,
        "price_mode": "Net",
        "vat_mode": "With VAT",
        "document_discount_percent": str(source.document_discount_percent or Decimal("0")),
        "shipping_net": str(source.shipping_net or Decimal("0")),
    }
    expected.update(flatten(source.customer, "customer"))
    expected.pop("customer.source_customer_id", None)
    if email_exception_applies(source.customer.email, email_policy):
        # The only exemption. The workflow records the persisted value as a typed warning.
        expected.pop("customer.email", None)
    expected.update(flatten(source.totals, "totals"))
    actual = flatten(observed)
    for path, value in expected.items():
        if value is None:
            continue
        seen = actual.get(path)
        numeric = path.startswith("totals.") or path in {
            "document_discount_percent",
            "shipping_net",
        }
        equal = seen is not None and (
            Decimal(str(value)) == Decimal(str(seen))
            if numeric
            else text_equal(str(value), str(seen))
        )
        if not equal:
            findings.append(
                Finding(
                    code="DOCUMENT_MISMATCH",
                    field=path,
                    message="Application value differs from source",
                    expected=str(value),
                    observed=str(seen),
                )
            )
    if len(source.items) != len(observed.items):
        findings.append(
            Finding(
                code="ROW_COUNT_MISMATCH",
                field="items",
                message="All rows must persist in source order",
            )
        )
    for index, (wanted, seen) in enumerate(zip(source.items, observed.items, strict=False)):
        for field in (
            "sku",
            "description",
            "quantity",
            "unit_net",
            "discount_percent",
            "vat_percent",
            "printed_net",
        ):
            left, right = getattr(wanted, field), getattr(seen, field)
            equal = text_equal(left, right) if isinstance(left, str) else left == right
            if not equal:
                findings.append(
                    Finding(
                        code="LINE_MISMATCH",
                        field=f"items.{index}.{field}",
                        message="Saved item differs from source",
                        expected=str(left),
                        observed=str(right),
                    )
                )
    if persisted and not observed.persisted:
        findings.append(
            Finding(
                code="NOT_PERSISTED",
                field="persisted",
                message="Editor state cannot establish persistence",
            )
        )
    if observed.kind == "Order" and observed.state != "open":
        findings.append(
            Finding(code="ORDER_NOT_OPEN", field="state", message="Source Order must remain open")
        )
    if observed.kind == "Invoice":
        paid = normalize(source.payment.status or "").upper() == "PAID"
        expected_states = {"paid"} if paid else {"open", "unpaid"}
        if persisted and observed.state not in expected_states:
            findings.append(
                Finding(
                    code="INVOICE_STATE_MISMATCH",
                    field="state",
                    message="The saved Invoice state differs from the source payment status",
                    expected="paid" if paid else "open or unpaid",
                    observed=observed.state,
                )
            )
        for field, value in {
            "payment_method": source.payment.method,
            "paid": paid,
            "payment_date": source.payment.payment_date if paid else None,
            "payment_value": source.totals.gross if paid else None,
        }.items():
            if getattr(observed, field) != value:
                findings.append(
                    Finding(
                        code="PAYMENT_MISMATCH",
                        field=field,
                        message="Invoice payment differs from source",
                        expected=str(value),
                        observed=str(getattr(observed, field)),
                    )
                )
    return findings
