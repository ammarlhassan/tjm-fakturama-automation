"""The approved exception: finish a reserved .test email, flag it, and relax nothing else."""

import json
from decimal import Decimal

import pytest

from image_to_cash.adapters import SimulatedAdapter
from image_to_cash.artifacts import source_identity
from image_to_cash.config import Settings
from image_to_cash.models import CompletionReceipt, Stage
from image_to_cash.reporting import build_report
from image_to_cash.validation import email_exception_applies, email_warning, verify_document
from image_to_cash.workflow import Workflow

FLAG, STRICT = "flag-reserved-domain", "strict"


class RejectsReservedEmail(SimulatedAdapter):
    """Models Fakturama 2.2.0: a new contact keeps a blank email for a .test address."""

    def create_debtor(self, source, payment_method):
        record = super().create_debtor(source, payment_method)
        record["customer"]["email"] = None
        self._persist("create_debtor_blank_email")
        return record


def job_for(store, source):
    job, _ = store.submit("demo", "a" * 64, "image.png", mode="simulation")
    store.update(job, source=source.model_dump(mode="json"), stage=Stage.VALIDATED)
    return job


def adapter_path(settings):
    return settings.workspace_path("demo") / "simulation.json"


@pytest.mark.parametrize(
    "email,policy,applies",
    [
        ("marta.klein@example.test", FLAG, True),
        ("MARTA@SUB.EXAMPLE.TEST ", FLAG, True),
        ("marta.klein@example.test", STRICT, False),
        ("marta.klein@example.com", FLAG, False),
        ("marta@test", FLAG, False),
        ("marta@example.test.com", FLAG, False),
        ("@example.test", FLAG, False),
        (None, FLAG, False),
        ("", FLAG, False),
    ],
)
def test_exception_is_limited_to_a_reserved_domain_under_the_flag_policy(email, policy, applies):
    assert email_exception_applies(email, policy) is applies


def test_unknown_policy_value_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setenv("ITC_EMAIL_POLICY", "anything-goes")
    with pytest.raises(ValueError):
        Settings(runtime=tmp_path)


def test_new_debtor_with_rejected_email_completes_with_a_blank_warning(store, settings, source):
    original = source.customer.email
    job = job_for(store, source)
    receipt = Workflow(settings, store, RejectsReservedEmail(adapter_path(settings))).run(job)
    assert receipt is not None, store.get(job)["review"]
    assert receipt.source_field_agreement is False
    warning = receipt.warnings[0]
    assert (warning.code, warning.field) == ("SOURCE_EMAIL_NOT_PERSISTED", "customer.email")
    assert warning.source_value == original
    assert warning.persisted_value is None and warning.persisted_state == "blank"
    assert warning.policy == FLAG
    assert "does not establish complete source-field agreement" in warning.message
    assert store.get(job)["source"]["customer"]["email"] == original
    assert receipt.totals.gross == Decimal("678.30") and receipt.paid
    assert "saved-contact-SIMULATED.json" in receipt.evidence
    actions = [event["action"] for event in store.events(job)]
    assert actions.index("observed_saved_contact") < actions.index("source_value_warning")


def test_existing_debtor_with_a_different_email_is_flagged_and_left_unchanged(
    store, settings, source
):
    existing = source.customer.model_copy(deep=True)
    existing.email = "marta.klein@example.com"
    adapter = SimulatedAdapter(adapter_path(settings))
    adapter.state["debtors"] = [{"id": "CUST000001", "customer": existing.model_dump(mode="json")}]
    adapter.state["payments"] = [
        {"id": "Bank Transfer", "name": "Bank Transfer", "code": "Credit transfer"}
    ]
    job = job_for(store, source)
    receipt = Workflow(settings, store, adapter).run(job)
    assert receipt is not None, store.get(job)["review"]
    assert receipt.warnings[0].persisted_value == "marta.klein@example.com"
    assert receipt.warnings[0].persisted_state == "different_existing_value"
    assert len(adapter.state["debtors"]) == 1
    assert adapter.state["debtors"][0]["customer"]["email"] == "marta.klein@example.com"


def test_reserved_email_that_did_persist_produces_no_warning(store, settings, source):
    job = job_for(store, source)
    receipt = Workflow(settings, store, SimulatedAdapter(adapter_path(settings))).run(job)
    assert receipt is not None and receipt.warnings == []
    assert receipt.source_field_agreement is True


def test_strict_policy_stops_the_same_input_before_any_financial_save(store, settings, source):
    settings.email_discrepancy_policy = STRICT
    job = job_for(store, source)
    adapter = RejectsReservedEmail(adapter_path(settings))
    assert Workflow(settings, store, adapter).run(job) is None
    assert store.get(job)["review"]["code"] == "DOCUMENT_VERIFICATION_FAILED"
    assert not adapter.state["documents"]


def test_ordinary_email_mismatch_still_stops_under_the_flag_policy(store, settings, source):
    source.customer.email = "marta.klein@example.com"
    job = job_for(store, source)
    adapter = RejectsReservedEmail(adapter_path(settings))
    assert Workflow(settings, store, adapter).run(job) is None
    findings = store.get(job)["review"]["findings"]
    assert [finding["field"] for finding in findings] == ["customer.email"]
    assert not adapter.state["documents"]


@pytest.mark.parametrize(
    "path,value",
    [
        (("customer", "telephone"), "+49 30 0000 0000"),
        (("customer", "alias"), "SOMEONE-ELSE"),
        (("customer", "billing", "street"), "Different Street 1"),
        (("customer", "delivery", "city"), "Hamburg"),
    ],
)
def test_other_contact_differences_still_stop_under_the_flag_policy(
    store, settings, source, path, value
):
    class Differs(RejectsReservedEmail):
        def create_debtor(self, customer, payment_method):
            record = super().create_debtor(customer, payment_method)
            target = record
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            return record

    job = job_for(store, source)
    adapter = Differs(adapter_path(settings))
    assert Workflow(settings, store, adapter).run(job) is None
    assert store.get(job)["review"]["code"] == "DOCUMENT_VERIFICATION_FAILED"
    assert not adapter.state["documents"]


def test_financial_and_payment_differences_still_stop_under_the_flag_policy(source):
    source_order = source.model_copy(deep=True)

    def observed():
        adapter_customer = source_order.customer.model_copy(deep=True)
        adapter_customer.email = None
        from datetime import date

        from image_to_cash.models import ObservedDocument

        return ObservedDocument(
            number="INV1",
            kind="Invoice",
            external_reference=source_order.external_reference,
            currency="EUR",
            order_date=source_order.order_date,
            document_date=date(2026, 11, 5),
            service_date=date(2026, 11, 5),
            customer=adapter_customer,
            items=[item.model_copy(deep=True) for item in source_order.items],
            totals=source_order.totals.model_copy(deep=True),
            payment_method="Bank Transfer",
            paid=True,
            payment_date=source_order.payment.payment_date,
            payment_value=source_order.totals.gross,
            persisted=True,
            state="paid",
        )

    clean = observed()
    assert verify_document(source_order, clean, persisted=True, email_policy=FLAG) == []
    assert [finding.field for finding in verify_document(source_order, clean, persisted=True)] == [
        "customer.email"
    ]

    wrong_total = observed()
    wrong_total.totals.gross = Decimal("678.31")
    assert verify_document(source_order, wrong_total, persisted=True, email_policy=FLAG)

    wrong_payment = observed()
    wrong_payment.payment_value = Decimal("1.00")
    assert verify_document(source_order, wrong_payment, persisted=True, email_policy=FLAG)

    wrong_row = observed()
    wrong_row.items[0].sku = "OTHER-SKU"
    assert verify_document(source_order, wrong_row, persisted=True, email_policy=FLAG)

    unsaved = observed()
    unsaved.persisted = False
    assert verify_document(source_order, unsaved, persisted=True, email_policy=FLAG)


def test_contact_email_changing_between_observations_stops_for_review(store, settings, source):
    class Unstable(RejectsReservedEmail):
        def observe_saved_contact(self, customer, destination):
            return {**super().observe_saved_contact(customer, destination), "email": "x@y.com"}

    job = job_for(store, source)
    assert Workflow(settings, store, Unstable(adapter_path(settings))).run(job) is None
    assert store.get(job)["review"]["code"] == "CONTACT_EMAIL_UNSTABLE"
    assert store.get(job)["receipt"] is None


def test_report_shows_both_values_and_disclaims_full_agreement(store, settings, source, tmp_path):
    job = job_for(store, source)
    Workflow(settings, store, RejectsReservedEmail(adapter_path(settings))).run(job)
    html = build_report(store.get(job), store.events(job), tmp_path).read_text(encoding="utf-8")
    assert "Completed with a flagged source discrepancy" in html
    assert "SOURCE_EMAIL_NOT_PERSISTED" in html
    assert source.customer.email in html
    assert "(blank)" in html
    assert "Complete source-field agreement is not claimed" in html


def test_report_without_a_warning_has_no_discrepancy_section(store, settings, source, tmp_path):
    job = job_for(store, source)
    Workflow(settings, store, SimulatedAdapter(adapter_path(settings))).run(job)
    html = build_report(store.get(job), store.events(job), tmp_path).read_text(encoding="utf-8")
    assert "flagged source discrepancy" not in html


def test_report_separates_extraction_retries_from_operator_review(store, source, tmp_path):
    job = job_for(store, source)
    html = build_report(store.get(job), store.events(job), tmp_path).read_text(encoding="utf-8")
    assert "explicit review resolution" not in html
    assert "extraction retry resolution" not in html

    store.log(job, Stage.NEEDS_REVIEW, "technical_review", "observed", {"source_changed": False})
    store.log(job, Stage.NEEDS_REVIEW, "technical_review", "observed", {"source_changed": False})
    html = build_report(store.get(job), store.events(job), tmp_path).read_text(encoding="utf-8")
    assert "2 extraction retry resolution(s)" in html
    assert "included operator review" not in html

    store.log(job, Stage.NEEDS_REVIEW, "human_review", "observed", {"reason": "Corrected a value"})
    html = build_report(store.get(job), store.events(job), tmp_path).read_text(encoding="utf-8")
    assert "1 explicit review resolution(s)" in html
    assert "included operator review" in html


def test_duplicate_run_returns_the_flagged_receipt_without_repeating_ui_work(
    store, settings, source
):
    job = job_for(store, source)
    adapter = RejectsReservedEmail(adapter_path(settings))
    workflow = Workflow(settings, store, adapter)
    first = workflow.run(job)
    calls = len(adapter.state["calls"])
    second = workflow.run(job)
    assert second == first and second.warnings == first.warnings
    assert len(adapter.state["calls"]) == calls


def test_warning_is_none_when_the_policy_does_not_apply():
    assert email_warning("a@example.com", None, FLAG) is None
    assert email_warning("a@example.test", None, STRICT) is None
    assert email_warning("a@example.test", " a@example.test ", FLAG) is None


def test_receipts_written_before_this_policy_still_load(source):
    historical = {
        "job_id": "older",
        "workspace": "integration",
        "mode": "live",
        "order_number": "PO000004",
        "invoice_number": "INV000004",
        "external_reference": "CLEAN-WIN-2026-C17",
        "totals": json.loads(source.totals.model_dump_json()),
        "payment_method": "Bank Transfer",
        "paid": True,
    }
    receipt = CompletionReceipt.model_validate(historical)
    assert receipt.warnings == [] and receipt.source_field_agreement is True


def test_source_identity_ignores_line_endings(tmp_path, monkeypatch):
    identity = source_identity()
    assert identity["package_version"] and len(identity["source_sha256"]) == 64
    assert source_identity() == identity
