from decimal import Decimal

import pytest

from image_to_cash.adapters import InjectedCrash, SimulatedAdapter
from image_to_cash.models import ReviewRequired, Stage
from image_to_cash.store import ConflictError
from image_to_cash.workflow import ABANDONED_DRAFT, Workflow


def job_for(store, source):
    job, _ = store.submit("demo", "a" * 64, "image.png", mode="simulation")
    store.update(job, source=source.model_dump(mode="json"), stage=Stage.VALIDATED)
    return job


@pytest.mark.parametrize("same_party", [False, True])
def test_fuzzy_contact_results_allow_distinct_parties_but_stop_address_conflicts(
    store, settings, source, same_party
):
    class BroadSearch(SimulatedAdapter):
        def debtor_candidates(self, customer):
            return self.state["debtors"]

    other = source.customer.model_copy(deep=True)
    if same_party:
        other.billing.postcode = "99999"
    else:
        other.company = "A different company returned by fuzzy search"
    adapter = BroadSearch(settings.workspace_path("demo") / "simulation.json")
    adapter.state["debtors"] = [{"id": "OTHER", "customer": other.model_dump(mode="json")}]
    job = job_for(store, source)
    receipt = Workflow(settings, store, adapter).run(job)
    if same_party:
        assert receipt is None and len(adapter.state["debtors"]) == 1
        assert store.get(job)["review"]["code"] == "DEBTOR_ADDRESS_CONFLICT"
    else:
        assert receipt is not None and len(adapter.state["debtors"]) == 2
    assert adapter.state["debtors"][0]["customer"] == other.model_dump(mode="json")


def test_fresh_workspace_full_flow(store, settings, source):
    job = job_for(store, source)
    adapter = SimulatedAdapter(settings.workspace_path("demo") / "simulation.json")
    receipt = Workflow(settings, store, adapter).run(job)
    assert receipt.mode == "simulation"
    assert receipt.totals.gross == Decimal("678.30")
    assert receipt.paid and receipt.payment_value == Decimal("678.30")
    assert len(adapter.state["documents"]) == 2
    assert len(adapter.state["products"]) == 2
    assert len(adapter.state["debtors"]) == 1
    assert adapter.state["documents"][0]["state"] == "open"
    assert adapter.state["invoice"]["source_order_number"] == receipt.order_number
    assert receipt.relationship_evidence["mechanism"] == "simulation_saved_parent"
    assert adapter.state["standard_payment"] == adapter.state["standard_vat"] == "unchanged"
    assert adapter.state["calls"].index("open_order") < adapter.state["calls"].index(
        "debtor_selector"
    )


def test_saved_relationship_failure_prevents_success_even_when_amounts_match(
    store, settings, source
):
    from image_to_cash.models import ReviewRequired

    class MissingRelationship(SimulatedAdapter):
        def relationship(self, *args):
            raise ReviewRequired(
                "SAVED_RELATIONSHIP_UNCONFIRMED", "Saved transaction grouping differs"
            )

    job = job_for(store, source)
    adapter = MissingRelationship(settings.workspace_path("demo") / "simulation.json")
    assert Workflow(settings, store, adapter).run(job) is None
    assert len(adapter.state["documents"]) == 2
    assert store.get(job)["stage"] == Stage.NEEDS_REVIEW
    assert store.get(job)["receipt"] is None
    assert store.worker_health()["workspaces"][0]["quarantined"]


@pytest.mark.parametrize(
    "point",
    [
        "create_payment",
        "create_debtor",
        "create_vat",
        "create_product",
        "select_product:0",
        "set_line:0",
        "save_order",
        "follow_up_invoice",
        "save_invoice",
    ],
)
def test_crash_reconciliation_does_not_duplicate(store, settings, source, point):
    job = job_for(store, source)
    path = settings.workspace_path("demo") / "simulation.json"
    with pytest.raises(InjectedCrash):
        Workflow(settings, store, SimulatedAdapter(path, crash_at=point)).run(job)
    recovered = SimulatedAdapter(path)
    receipt = Workflow(settings, store, recovered).run(job, resume=True)
    assert receipt is not None, store.get(job)["review"]
    assert len(recovered.state["documents"]) == 2
    assert len(recovered.state["debtors"]) == 1
    assert len(recovered.state["products"]) == 2
    assert len(recovered.state["payments"]) == 1
    assert len(recovered.state["vats"]) == 1


def test_unpaid_is_never_paid_by_substring(store, settings, source):
    source.payment.status = "UNPAID"
    source.payment.payment_date = None
    job = job_for(store, source)
    receipt = Workflow(
        settings, store, SimulatedAdapter(settings.workspace_path("demo") / "simulation.json")
    ).run(job)
    assert not receipt.paid
    assert receipt.payment_value is None and receipt.payment_date is None


@pytest.mark.parametrize("point", ["set_line:0", "save_order"])
def test_resume_rejects_changed_order_number_before_invoice(store, settings, source, point):
    job = job_for(store, source)
    path = settings.workspace_path("demo") / "simulation.json"
    with pytest.raises(InjectedCrash):
        Workflow(settings, store, SimulatedAdapter(path, crash_at=point)).run(job)
    adapter = SimulatedAdapter(path)
    adapter.state["order"]["number"] = "ANOTHER-ORDER"
    for document in adapter.state["documents"]:
        if document["kind"] == "Order":
            document["number"] = "ANOTHER-ORDER"
    adapter._persist("external_operator_change")

    assert Workflow(settings, store, adapter).run(job, resume=True) is None
    assert store.get(job)["review"]["code"] == "ORDER_NUMBER_CHANGED"
    assert "follow_up_invoice" not in adapter.state["calls"]
    assert len(adapter.state["documents"]) == (1 if point == "save_order" else 0)


@pytest.mark.parametrize("point", ["set_payment", "save_invoice"])
def test_resume_rejects_changed_invoice_number(store, settings, source, point):
    job = job_for(store, source)
    path = settings.workspace_path("demo") / "simulation.json"
    with pytest.raises(InjectedCrash):
        Workflow(settings, store, SimulatedAdapter(path, crash_at=point)).run(job)
    adapter = SimulatedAdapter(path)
    adapter.state["invoice"]["number"] = "ANOTHER-INVOICE"
    for document in adapter.state["documents"]:
        if document["kind"] == "Invoice":
            document["number"] = "ANOTHER-INVOICE"
    adapter._persist("external_operator_change")

    assert Workflow(settings, store, adapter).run(job, resume=True) is None
    assert store.get(job)["review"]["code"] == "GENERATED_FIELDS_CHANGED"
    assert adapter.state["calls"].count("follow_up_invoice") == 1


def test_resume_rejects_an_invoice_created_outside_this_job(store, settings, source):
    job = job_for(store, source)
    path = settings.workspace_path("demo") / "simulation.json"
    with pytest.raises(InjectedCrash):
        Workflow(settings, store, SimulatedAdapter(path, crash_at="save_order")).run(job)
    adapter = SimulatedAdapter(path)
    adapter.follow_up_invoice(adapter.read_order())
    adapter.set_payment(source)
    adapter.save_invoice()

    assert Workflow(settings, store, adapter).run(job, resume=True) is None
    assert store.get(job)["review"]["code"] == "UNTRACKED_EXISTING_INVOICE"
    assert adapter.state["calls"].count("follow_up_invoice") == 1


def test_order_identity_uses_the_latest_draft_observation(store, settings, source):
    job = job_for(store, source)
    store.log(job, Stage.ORDER_OPEN, "open_order", "observed", {"number": "DISCARDED-DRAFT"})
    adapter = SimulatedAdapter(settings.workspace_path("demo") / "simulation.json")
    receipt = Workflow(settings, store, adapter).run(job)
    assert receipt is not None, store.get(job)["review"]
    assert receipt.order_number != "DISCARDED-DRAFT"


def test_duplicate_master_stops_and_quarantines_workspace(store, settings, source):
    job = job_for(store, source)
    adapter = SimulatedAdapter(settings.workspace_path("demo") / "simulation.json")
    adapter.state["debtors"] = [
        {"id": str(i), "customer": source.customer.model_dump(mode="json")} for i in range(2)
    ]
    assert Workflow(settings, store, adapter).run(job) is None
    assert store.get(job)["stage"] == Stage.NEEDS_REVIEW
    assert store.worker_health()["workspaces"][0]["quarantined"]


def test_completed_job_is_idempotent(store, settings, source):
    job = job_for(store, source)
    adapter = SimulatedAdapter(settings.workspace_path("demo") / "simulation.json")
    workflow = Workflow(settings, store, adapter)
    first = workflow.run(job)
    before = len(adapter.state["calls"])
    assert workflow.run(job) == first
    assert len(adapter.state["calls"]) == before


def test_wrong_application_currency_prevents_financial_save(store, settings, source):
    class WrongCurrency(SimulatedAdapter):
        def read_order(self):
            observed = super().read_order()
            observed.currency = "AFN"
            return observed

    job = job_for(store, source)
    adapter = WrongCurrency(settings.workspace_path("demo") / "simulation.json")
    assert Workflow(settings, store, adapter).run(job) is None
    assert store.get(job)["review"]["code"] == "DOCUMENT_VERIFICATION_FAILED"
    assert not adapter.state["documents"]
    assert "save_order" not in adapter.state["calls"]


def test_resume_keeps_invoice_draft_when_application_cannot_reopen_source_order(
    store, settings, source
):
    class DraftOriginCollision(SimulatedAdapter):
        def prepare(self):
            return {"defer_order_reopen_during_invoice_draft": True}

        def current_invoice(self, order_number):
            invoice = super().current_invoice(order_number)
            if invoice and not invoice.persisted:
                invoice.relationship_evidence.update(editor_new=True, editor_dirty=True)
            return invoice

        def saved(self, kind, reference):
            pending = self.state["invoice"]
            if kind == "Order" and pending and not pending["persisted"]:
                raise AssertionError(
                    "Fakturama cannot reopen the source while its follow-up draft is open"
                )
            return super().saved(kind, reference)

    job = job_for(store, source)
    path = settings.workspace_path("demo") / "simulation.json"
    with pytest.raises(InjectedCrash):
        Workflow(settings, store, DraftOriginCollision(path, crash_at="set_payment")).run(job)
    adapter = DraftOriginCollision(path)
    receipt = Workflow(settings, store, adapter).run(job, resume=True)
    assert receipt is not None, store.get(job)["review"]
    assert adapter.state["calls"].count("follow_up_invoice") == 1
    assert len(adapter.state["documents"]) == 2
    assert any(event["action"] == "deferred_order_reopen" for event in store.events(job))


def test_resume_rejects_invoice_dates_changed_since_generated_observation(store, settings, source):
    job = job_for(store, source)
    path = settings.workspace_path("demo") / "simulation.json"
    with pytest.raises(InjectedCrash):
        Workflow(settings, store, SimulatedAdapter(path, crash_at="set_payment")).run(job)
    adapter = SimulatedAdapter(path)
    adapter.state["invoice"]["document_date"] = "2020-01-01"
    adapter._persist("external_operator_change")
    assert Workflow(settings, store, adapter).run(job, resume=True) is None
    assert store.get(job)["review"]["code"] == "GENERATED_FIELDS_CHANGED"
    assert len(adapter.state["documents"]) == 1


def stopped_on_open_draft(store, settings, source):
    class StopsAfterOpen(SimulatedAdapter):
        def debtor_candidates(self, customer):
            raise ReviewRequired("TABLE_EXPORT_PROFILE_UNKNOWN", "Stopped at a selector")

    job = job_for(store, source)
    path = settings.workspace_path("demo") / "simulation.json"
    assert Workflow(settings, store, StopsAfterOpen(path)).run(job) is None
    assert store.worker_health()["workspaces"][0]["quarantined"]
    return job, SimulatedAdapter(path)


def test_abandon_discards_identified_draft_and_releases_quarantine(store, settings, source):
    job, adapter = stopped_on_open_draft(store, settings, source)
    result = Workflow(settings, store, adapter).abandon_draft(job, "Benchmark stopped; no Save")
    assert result["draft_present"] and result["saved_orders_and_invoices_after_discard"] == 0
    assert adapter.state["order"] is None
    assert store.worker_health()["workspaces"] == []
    record = store.get(job)
    assert record["stage"] == Stage.FAILED and record["review"]["code"] == ABANDONED_DRAFT
    assert record["review"]["prior_review"]["code"] == "TABLE_EXPORT_PROFILE_UNKNOWN"
    actions = [(e["action"], e["status"]) for e in store.events(job)]
    assert actions[-2:] == [("abandon_owned_draft", "intent"), ("abandon_owned_draft", "observed")]


def test_abandon_refuses_a_job_with_any_save_intent(store, settings, source):
    job = job_for(store, source)
    path = settings.workspace_path("demo") / "simulation.json"
    with pytest.raises(InjectedCrash):
        Workflow(settings, store, SimulatedAdapter(path, crash_at="save_order")).run(job)
    store.update(job, stage=Stage.NEEDS_REVIEW)
    adapter = SimulatedAdapter(path)
    with pytest.raises(ConflictError, match="resume to reconcile"):
        Workflow(settings, store, adapter).abandon_draft(job, "Attempted after Save")
    assert len(adapter.state["documents"]) == 1


def test_abandon_keeps_quarantine_when_saved_documents_exist(store, settings, source):
    job, adapter = stopped_on_open_draft(store, settings, source)
    adapter.state["documents"] = [
        {**adapter.state["order"], "persisted": True},
    ]
    with pytest.raises(ConflictError, match="Saved documents exist"):
        Workflow(settings, store, adapter).abandon_draft(job, "Must not discard")
    assert adapter.state["order"] is not None
    assert store.worker_health()["workspaces"][0]["quarantined"]
    assert store.get(job)["stage"] == Stage.NEEDS_REVIEW


def test_abandon_keeps_quarantine_when_draft_identity_differs(store, settings, source):
    job, adapter = stopped_on_open_draft(store, settings, source)
    adapter.state["order"]["number"] = "SOMEONE-ELSES-DRAFT"
    with pytest.raises(ReviewRequired) as raised:
        Workflow(settings, store, adapter).abandon_draft(job, "Must not discard")
    assert raised.value.code == "DRAFT_IDENTITY_UNCONFIRMED"
    assert adapter.state["order"] is not None
    assert store.worker_health()["workspaces"][0]["quarantined"]


def test_repeated_sku_keeps_source_rows_and_transaction_prices(store, settings, source):
    from image_to_cash.validation import calculate, line_net

    source.items[1].sku = source.items[0].sku
    source.items[1].quantity = Decimal("1.5")
    source.items[1].unit_net = Decimal("37.50")
    source.items[1].printed_net = line_net(source.items[1])
    source.totals = calculate(source)
    job = job_for(store, source)
    adapter = SimulatedAdapter(settings.workspace_path("demo") / "simulation.json")
    receipt = Workflow(settings, store, adapter).run(job)
    assert receipt is not None, store.get(job)["review"]
    assert len(adapter.state["products"]) == 1
    assert adapter.state["calls"].count("product_selector:CHR-ERG-01") >= 2
    invoice = adapter.state["invoice"]
    assert [row["sku"] for row in invoice["items"]] == ["CHR-ERG-01", "CHR-ERG-01"]
    assert [Decimal(row["unit_net"]) for row in invoice["items"]] == [
        Decimal("250"),
        Decimal("37.50"),
    ]
    assert Decimal(invoice["items"][1]["quantity"]) == Decimal("1.5")
