from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from image_to_cash.models import ItemInput, ObservedDocument, OrderInput
from image_to_cash.validation import (
    calculate,
    documents_row_date,
    documents_row_findings,
    line_net,
    master_gross,
    validate_order,
    verify_document,
)


def test_visible_sample(source):
    report = validate_order(source)
    assert report.accepted
    assert report.computed.model_dump(mode="json") == {
        "net": "570.00",
        "vat": "108.30",
        "gross": "678.30",
    }
    assert master_gross(source.items[0]) == Decimal("297.50")
    assert master_gross(source.items[1]) == Decimal("47.60")


def test_master_price_does_not_include_line_discount(source):
    assert line_net(source.items[0]) == Decimal("450.00")
    assert master_gross(source.items[0]) == Decimal("297.50")


def test_float_money_is_rejected():
    with pytest.raises(ValidationError):
        ItemInput(unit_net=0.1)


def test_decimal_serialization_and_postcode_leading_zero(source):
    source.customer.billing.postcode = "01067"
    round_trip = OrderInput.model_validate_json(source.model_dump_json())
    assert round_trip.customer.billing.postcode == "01067"
    assert '"unit_net":"250.00"' in round_trip.model_dump_json()


def test_wrong_printed_totals_stop(source):
    source.totals.gross = Decimal("678.31")
    assert any(f.code == "TOTAL_MISMATCH" for f in validate_order(source).findings)


def test_matching_grand_total_does_not_hide_wrong_line(source):
    source.items[0].printed_net = Decimal("449.00")
    source.items[1].printed_net = Decimal("121.00")
    assert len([f for f in validate_order(source).findings if f.code == "LINE_TOTAL_MISMATCH"]) == 2


def test_paid_requires_source_date(source):
    source.payment.payment_date = None
    assert any(f.code == "PAYMENT_DATE_MISSING" for f in validate_order(source).findings)


def test_partial_payment_is_reviewed(source):
    source.payment.status = "PARTIALLY PAID"
    assert any(f.code == "PAYMENT_STATUS_AMBIGUOUS" for f in validate_order(source).findings)


def test_mixed_vat_and_fractional_quantity(source):
    source.items[0].quantity = Decimal("1.5")
    source.items[0].printed_net = Decimal("337.50")
    source.items[1].vat_percent = Decimal("7")
    source.totals = calculate(source)
    assert source.totals.net == Decimal("457.50")
    assert source.totals.vat == Decimal("72.53")
    assert validate_order(source).accepted


def test_rounding_boundary_and_negative_quantity(source):
    item = source.items[0]
    item.quantity, item.unit_net, item.discount_percent = (
        Decimal("1"),
        Decimal("0.005"),
        Decimal("0"),
    )
    assert line_net(item) == Decimal("0.01")
    item.quantity = Decimal("-1")
    assert any(f.code == "INVALID_QUANTITY" for f in validate_order(source).findings)


def test_shipping_requires_explicit_tax(source):
    source.shipping_net = Decimal("10")
    assert any(f.code == "SHIPPING_VAT_MISSING" for f in validate_order(source).findings)


def saved_invoice(source, **row):
    return ObservedDocument(
        number="INV000011",
        kind="Invoice",
        external_reference=source.external_reference,
        currency="EUR",
        order_date=source.order_date,
        document_date=date(2026, 11, 5),
        service_date=date(2026, 11, 5),
        customer=source.customer.model_copy(deep=True),
        items=[item.model_copy(deep=True) for item in source.items],
        totals=source.totals.model_copy(deep=True),
        payment_method="Bank Transfer",
        paid=True,
        payment_date=source.payment.payment_date,
        payment_value=source.totals.gross,
        persisted=True,
        state="paid",
        documents_row={
            "document": "INV000011",
            "date": "Thu Nov 05 00:00:00 EET 2026",
            "custref": source.external_reference,
            "state": "COMMAND_CHECKED",
            "total": "678.3",
            **row,
        },
    )


def test_matching_documents_row_passes_with_its_short_decimal_total(source):
    assert verify_document(source, saved_invoice(source), persisted=True) == []


@pytest.mark.parametrize(
    "row",
    [
        {"document": "INV000012"},
        {"custref": "ANOTHER-REFERENCE"},
        {"date": "Fri Nov 06 00:00:00 EET 2026"},
        {"date": "not a date"},
        {"total": "678.31"},
        {"total": "unreadable"},
    ],
)
def test_documents_row_difference_stops(source, row):
    findings = verify_document(source, saved_invoice(source, **row), persisted=True)
    assert [finding.code for finding in findings] == ["DOCUMENTS_ROW_MISMATCH"]


def test_order_row_date_is_compared_across_daylight_saving_labels(source):
    order = saved_invoice(source, date="Tue Jul 14 00:00:00 EEST 2026")
    order.document_date = date(2026, 7, 14)
    assert documents_row_findings(order) == []
    assert documents_row_date("Tue Jul 14 00:00:00 EEST 2026") == date(2026, 7, 14)


def test_saved_invoice_state_must_reflect_the_source_payment_status(source):
    invoice = saved_invoice(source)
    invoice.state = "open"
    assert any(
        f.code == "INVOICE_STATE_MISMATCH" for f in verify_document(source, invoice, persisted=True)
    )
    assert not any(
        f.code == "INVOICE_STATE_MISMATCH"
        for f in verify_document(source, invoice, persisted=False)
    )


@pytest.mark.parametrize("state", ["unknown-state", "cancelled", "", "paid", "open", "unpaid"])
def test_unpaid_invoice_requires_a_recognized_saved_state(source, state):
    source.payment.status = "UNPAID"
    source.payment.payment_date = None
    invoice = saved_invoice(source)
    invoice.state = state
    invoice.paid = False
    invoice.payment_date = None
    invoice.payment_value = None
    invoice.documents_row = {}
    findings = verify_document(source, invoice, persisted=True)
    assert bool([f for f in findings if f.code == "INVOICE_STATE_MISMATCH"]) == (
        state not in {"open", "unpaid"}
    )
