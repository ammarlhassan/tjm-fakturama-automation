from decimal import Decimal

from image_to_cash.evaluation import differences


def test_accuracy_preserves_postcode_and_numeric_identifier_spelling(source):
    actual = source.model_copy(deep=True)
    source.customer.billing.postcode = "01067"
    actual.customer.billing.postcode = "1067"
    source.items[0].sku = "001"
    actual.items[0].sku = "1"
    assert {item["field"] for item in differences(source, actual)} == {
        "customer.billing.postcode",
        "items.0.sku",
    }


def test_accuracy_normalizes_decimal_formatting_only(source):
    actual = source.model_copy(deep=True)
    actual.items[0].unit_net = Decimal("250.0000")
    assert differences(source, actual) == []


def test_accuracy_counts_an_invented_optional_printed_value(source):
    actual = source.model_copy(deep=True)
    assert source.shipping_net is None
    actual.shipping_net = Decimal("0")
    assert differences(source, actual) == [
        {"field": "shipping_net", "expected": None, "observed": "0"}
    ]
