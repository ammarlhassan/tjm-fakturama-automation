"""Shared contracts. Source values are distinct from observed application state."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, WithJsonSchema


def utcnow() -> datetime:
    return datetime.now(UTC)


def decimal_string(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        result = Decimal(value)
    else:
        raise ValueError("Use a canonical decimal string, e.g. '250.00'; floats are not accepted")
    if not result.is_finite():
        raise ValueError("Decimal must be finite")
    return result


DecimalString = Annotated[
    Decimal,
    BeforeValidator(decimal_string),
    PlainSerializer(lambda v: format(v, "f"), return_type=str),
    WithJsonSchema({"type": "string", "pattern": r"^-?\d+(\.\d+)?$"}),
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Address(Contract):
    recipient: str | None = None
    additional_name: str | None = None
    street: str | None = None
    postcode: str | None = None
    city: str | None = None
    country: str | None = None


class Customer(Contract):
    company: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    alias: str | None = None
    email: str | None = None
    telephone: str | None = None
    source_customer_id: str | None = None
    billing: Address = Field(default_factory=Address)
    delivery: Address = Field(default_factory=Address)


class PaymentInput(Contract):
    method: str | None = None
    status: str | None = None
    payment_date: date | None = None


class ItemInput(Contract):
    sku: str | None = None
    description: str | None = None
    quantity: DecimalString | None = None
    unit: str | None = None
    unit_net: DecimalString | None = None
    discount_percent: DecimalString | None = None
    vat_percent: DecimalString | None = None
    printed_net: DecimalString | None = None


class Totals(Contract):
    net: DecimalString | None = None
    vat: DecimalString | None = None
    gross: DecimalString | None = None


class OrderInput(Contract):
    order_date: date | None = None
    external_reference: str | None = None
    currency: str | None = None
    customer: Customer = Field(default_factory=Customer)
    payment: PaymentInput = Field(default_factory=PaymentInput)
    items: list[ItemInput] = Field(default_factory=list)
    totals: Totals = Field(default_factory=Totals)
    document_discount_percent: DecimalString | None = None
    shipping_net: DecimalString | None = None
    shipping_vat_percent: DecimalString | None = None


class Box(Contract):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class FieldEvidence(Contract):
    field: str
    raw_text: str
    engine: str
    boxes: list[Box] = Field(default_factory=list)
    confidence: float | None = None
    artifact: str | None = None


class ExtractionResult(Contract):
    order: OrderInput
    evidence: list[FieldEvidence] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class Finding(Contract):
    code: str
    field: str
    message: str
    expected: str | None = None
    observed: str | None = None


class ValidationReport(Contract):
    findings: list[Finding] = Field(default_factory=list)
    computed: Totals | None = None
    profile: str

    @property
    def accepted(self) -> bool:
        return not self.findings


class Stage(StrEnum):
    RECEIVED = "RECEIVED"
    EXTRACTED = "EXTRACTED"
    VALIDATED = "VALIDATED"
    ORDER_OPEN = "ORDER_OPEN"
    DEBTOR_READY = "DEBTOR_READY"
    ITEMS_READY = "ITEMS_READY"
    ORDER_SAVED_VERIFIED = "ORDER_SAVED_VERIFIED"
    INVOICE_OPEN = "INVOICE_OPEN"
    INVOICE_SAVED_VERIFIED = "INVOICE_SAVED_VERIFIED"
    SUCCEEDED = "SUCCEEDED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    RECONCILING = "RECONCILING"
    FAILED = "FAILED"


class WorkflowCheckpoint(Contract):
    stage: Stage
    action: str
    status: str
    observed: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utcnow)


class ObservedDocument(Contract):
    number: str
    kind: str
    external_reference: str
    currency: str | None = None
    displayed_item_subtotal: DecimalString | None = None
    net_is_derived: bool = False
    order_date: date
    document_date: date
    service_date: date
    price_mode: str = "Net"
    vat_mode: str = "With VAT"
    customer: Customer
    items: list[ItemInput]
    totals: Totals
    document_discount_percent: DecimalString = Decimal("0")
    shipping_net: DecimalString = Decimal("0")
    source_order_number: str | None = None
    relationship_evidence: dict = Field(default_factory=dict)
    application_notices: list[str] = Field(default_factory=list)
    state: str = "open"
    payment_method: str | None = None
    paid: bool = False
    payment_date: date | None = None
    payment_value: DecimalString | None = None
    persisted: bool = False
    documents_row: dict[str, str] = Field(default_factory=dict)


class SourceValueWarning(Contract):
    """A source value the application did not persist; the source itself stays unchanged."""

    code: str
    field: str
    source_value: str | None = None
    persisted_value: str | None = None
    persisted_state: str
    policy: str
    message: str


class CompletionReceipt(Contract):
    job_id: str
    workspace: str
    mode: str
    order_number: str
    invoice_number: str
    debtor_number: str | None = None
    order_date: date | None = None
    invoice_date: date | None = None
    invoice_service_date: date | None = None
    source_order_state: str | None = None
    relationship_evidence: dict = Field(default_factory=dict)
    external_reference: str
    currency: str | None = None
    extraction_provenance: dict = Field(default_factory=dict)
    totals: Totals
    payment_method: str
    paid: bool
    payment_date: date | None = None
    payment_value: DecimalString | None = None
    verified_at: datetime = Field(default_factory=utcnow)
    evidence: list[str] = Field(default_factory=list)
    warnings: list[SourceValueWarning] = Field(default_factory=list)
    source_field_agreement: bool = True
    build: dict = Field(default_factory=dict)


class ReviewRequired(Exception):
    def __init__(self, code: str, message: str, *, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}
