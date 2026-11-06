from __future__ import annotations

import platform
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from decimal import Decimal

from filelock import FileLock, Timeout

from .adapters import DesktopAdapter, exact_debtor, same_debtor_identity
from .artifacts import source_identity, write_json
from .config import Settings
from .models import CompletionReceipt, ObservedDocument, OrderInput, ReviewRequired, Stage
from .store import ConflictError, Store
from .validation import (
    PAYMENT_CODES,
    email_exception_applies,
    email_warning,
    normalize,
    validate_order,
    verify_document,
)

ABANDONED_DRAFT = "OWNED_DRAFT_ABANDONED"


class Workflow:
    def __init__(self, settings: Settings, store: Store, adapter: DesktopAdapter):
        self.settings, self.store, self.adapter = settings, store, adapter
        self.worker = f"{platform.node()}:{uuid.uuid4()}"
        self.ui_started = False

    def _mutate(self, job_id: str, stage: Stage, action: str, function: Callable, **intent):
        self.store.log(job_id, stage, action, "intent", intent)
        result = function()
        data = (
            result.model_dump(mode="json")
            if hasattr(result, "model_dump")
            else result
            if isinstance(result, dict)
            else {}
        )
        self.store.log(job_id, stage, action, "observed", data)
        return result

    def _verify(self, source: OrderInput, document: ObservedDocument, *, persisted: bool):
        findings = verify_document(
            source,
            document,
            persisted=persisted,
            email_policy=self.settings.email_discrepancy_policy,
        )
        if findings:
            raise ReviewRequired(
                "DOCUMENT_VERIFICATION_FAILED",
                "Application values do not match the source",
                evidence={"findings": [f.model_dump() for f in findings]},
            )

    def _single_saved(self, kind, reference):
        records = self.adapter.saved(kind, reference)
        if len(records) > 1:
            raise ReviewRequired(
                "DUPLICATE_DOCUMENTS", f"Multiple saved {kind}s have the source reference"
            )
        return records[0] if records else None

    def _restore_lineage(self, job_id, invoice, order):
        if invoice.source_order_number is not None:
            return invoice
        observations = [
            event
            for event in self.store.events(job_id)
            if event["action"] == "follow_up_invoice"
            and event["status"] == "observed"
            and event["data"].get("number") == invoice.number
            and event["data"].get("source_order_number") == order.number
            and event["data"].get("external_reference") == invoice.external_reference
        ]
        if observations:
            invoice.source_order_number = order.number
            invoice.relationship_evidence = {
                "mechanism": "journal_of_observed_fakturama_follow_up",
                "event_id": observations[-1]["id"],
                "order_number": order.number,
                "invoice_number": invoice.number,
            }
        return invoice

    def _verified_order_checkpoint(self, job_id):
        observations = [
            event
            for event in self.store.events(job_id)
            if event["action"] == "verified_order" and event["status"] == "observed"
        ]
        return (
            ObservedDocument.model_validate(observations[-1]["data"]["document"])
            if observations
            else None
        )

    def _source_value_warnings(self, job_id, source, documents, output):
        """Record, from a reopened saved contact, what happened to a reserved source email."""
        policy = self.settings.email_discrepancy_policy
        if not email_exception_applies(source.customer.email, policy):
            return [], []
        contact = self.adapter.observe_saved_contact(source.customer, output)
        self.store.log(
            job_id, Stage.INVOICE_SAVED_VERIFIED, "observed_saved_contact", "observed", contact
        )
        seen_on_documents = {normalize(document.customer.email or "") for document in documents}
        if seen_on_documents != {normalize(contact["email"] or "")}:
            raise ReviewRequired(
                "CONTACT_EMAIL_UNSTABLE",
                "The reopened Debtor's email differs from the value observed while verifying documents",
                evidence={"documents": sorted(seen_on_documents), "contact": contact["email"]},
            )
        warning = email_warning(source.customer.email, contact["email"], policy)
        if warning is None:
            return [], contact["evidence"]
        self.store.log(
            job_id,
            Stage.INVOICE_SAVED_VERIFIED,
            "source_value_warning",
            "observed",
            warning.model_dump(mode="json"),
        )
        return [warning], contact["evidence"]

    def _verify_generated_invoice(self, job_id, invoice):
        observations = [
            event["data"]
            for event in self.store.events(job_id)
            if event["status"] == "observed"
            and (
                (
                    event["action"] == "follow_up_invoice"
                    and event["data"].get("external_reference") == invoice.external_reference
                )
                or (event["action"] == "transition" and event["stage"] == Stage.INVOICE_OPEN)
            )
        ]
        if observations:
            original = observations[-1]
            if (
                original.get("number") != invoice.number
                or original.get("document_date") != invoice.document_date.isoformat()
                or original.get("service_date") != invoice.service_date.isoformat()
            ):
                raise ReviewRequired(
                    "GENERATED_FIELDS_CHANGED",
                    "Invoice identifiers or dates differ from this job's generated values",
                )
        elif invoice.persisted or not any(
            event["action"] == "follow_up_invoice"
            and event["status"] == "intent"
            and event["data"].get("order_number") == invoice.source_order_number
            for event in self.store.events(job_id)
        ):
            raise ReviewRequired(
                "UNTRACKED_EXISTING_INVOICE",
                "Invoice has no matching generated observation or recoverable follow-up intent",
            )

    def _verify_generated_order(self, job_id, order):
        observations = [
            event["data"]
            for event in self.store.events(job_id)
            if (
                (event["action"] == "open_order" and event["status"] == "observed")
                or (event["action"] == "save_order" and event["status"] == "intent")
            )
            and event["data"].get("number")
        ]
        if not observations:
            raise ReviewRequired(
                "UNTRACKED_EXISTING_ORDER", "Order has no generated number observed by this job"
            )
        if order.number != observations[-1]["number"]:
            raise ReviewRequired(
                "ORDER_NUMBER_CHANGED",
                "Order number differs from this job's latest generated number",
            )

    @contextmanager
    def _ownership(self, job_id: str, workspace: str, resume: bool):
        lock = FileLock(str(self.settings.runtime / "locks" / f"job-{job_id}.lock"), timeout=0)
        desktop = FileLock(
            str(
                self.settings.runtime
                / "locks"
                / (
                    "interactive-desktop.lock"
                    if self.adapter.mode == "live"
                    else f"simulation-{workspace}.lock"
                )
            ),
            timeout=0,
        )
        workspace_lock = FileLock(
            str(self.settings.runtime / "locks" / f"workspace-{workspace}.lock"), timeout=0
        )
        try:
            with lock, desktop, workspace_lock:
                job = self.store.get(job_id)
                if resume and job["claimed_by"]:
                    # Explicit local resume, with the OS locks already acquired. No lease-expiry takeover.
                    if not self.settings.db_url.startswith("sqlite"):
                        raise ConflictError(
                            "Distributed abandoned claims require operator reconciliation"
                        )
                    self.store.log(
                        job_id,
                        Stage.RECONCILING,
                        "recover_local_claim",
                        "observed",
                        {"previous_owner": job["claimed_by"]},
                    )
                    self.store.update(job_id, claimed_by=None, claimed_at=None)
                if not self.store.claim_job(job_id, self.worker):
                    raise ConflictError("Another worker owns this job")
                acquired = False
                complete = False
                try:
                    self.store.own_workspace(
                        workspace, job_id, self.worker, desktop=self.adapter.mode == "live"
                    )
                    acquired = True
                    stop = threading.Event()

                    def pulse():
                        while not stop.wait(10):
                            self.store.heartbeat(workspace, job_id)

                    heartbeat = threading.Thread(target=pulse, daemon=True)
                    heartbeat.start()
                    try:
                        yield
                        complete = self.store.get(job_id)["stage"] == Stage.SUCCEEDED
                    finally:
                        stop.set()
                        heartbeat.join(timeout=2)
                finally:
                    if acquired:
                        self.store.release_workspace(
                            workspace, job_id, quarantine=self.ui_started and not complete
                        )
                    self.store.release_job(job_id, self.worker)
        except Timeout as exc:
            raise ConflictError("Job, desktop session, or workspace is busy") from exc

    def abandon_draft(self, job_id: str, reason: str) -> dict:
        """Release a quarantine only by discarding this job's identified, unsaved draft."""
        job = self.store.get(job_id)
        if job["stage"] != Stage.NEEDS_REVIEW or job["receipt"]:
            raise ConflictError("Only an unfinished job awaiting review can abandon its draft")
        events = self.store.events(job_id)
        if any(
            event["action"] in {"save_order", "follow_up_invoice", "save_invoice"}
            for event in events
        ):
            raise ConflictError(
                "A financial Save or follow-up may have occurred; resume to reconcile saved records"
            )
        opened = [
            event
            for event in events
            if event["action"] == "open_order" and event["status"] == "observed"
        ]
        reference = job["external_reference"] or (job["source"] or {}).get("external_reference")
        if not opened or not reference:
            raise ConflictError("This job has no observed draft to abandon")
        number = opened[-1]["data"]["number"]

        def saved_numbers():
            return [
                document.number
                for kind in ("Order", "Invoice")
                for document in self.adapter.saved(kind, reference)
            ]

        with self._ownership(job_id, job["workspace"], True):
            # Any failure below keeps the quarantine: application state is then unconfirmed.
            self.ui_started = True
            existing = saved_numbers()
            if existing:
                raise ConflictError(
                    f"Saved documents exist for {reference}: {existing}; resume to reconcile them"
                )
            self.store.log(
                job_id,
                Stage.NEEDS_REVIEW,
                "abandon_owned_draft",
                "intent",
                {"number": number, "external_reference": reference, "reason": reason},
            )
            observed = self.adapter.discard_order_draft(number, reference)
            remaining = saved_numbers()
            if remaining:
                raise ConflictError(f"Saved documents appeared for {reference}: {remaining}")
            result = {
                **observed,
                "reason": reason,
                "saved_orders_and_invoices_after_discard": 0,
                "prior_review": job["review"],
            }
            self.store.log(job_id, Stage.NEEDS_REVIEW, "abandon_owned_draft", "observed", result)
            self.store.update(
                job_id,
                stage=Stage.FAILED,
                review={
                    "code": ABANDONED_DRAFT,
                    "message": "Unsaved draft discarded under explicit reconciliation. "
                    "No Order or Invoice was saved. This job cannot be resumed.",
                    "resolved": True,
                    "resolution": reason,
                    "prior_review": job["review"],
                },
            )
            self.ui_started = False
            return result

    def run(self, job_id: str, *, resume: bool = False) -> CompletionReceipt | None:
        job = self.store.get(job_id)
        if job["stage"] == Stage.SUCCEEDED:
            return CompletionReceipt.model_validate(job["receipt"])
        if job["stage"] == Stage.NEEDS_REVIEW and not resume:
            raise ConflictError("Record a review resolution and explicitly resume this job")
        if not job["source"]:
            raise ReviewRequired(
                "SOURCE_MISSING", "Extraction and source validation must finish first"
            )
        source = OrderInput.model_validate(job["source"])
        validation = validate_order(source)
        if not validation.accepted:
            self.store.update(
                job_id,
                stage=Stage.NEEDS_REVIEW,
                review={
                    "code": "SOURCE_VALIDATION_FAILED",
                    "findings": [f.model_dump() for f in validation.findings],
                },
            )
            return None
        output = self.settings.runtime / "jobs" / job_id
        output.mkdir(parents=True, exist_ok=True)
        try:
            with self._ownership(job_id, job["workspace"], resume):
                self.ui_started = True
                prepared = self._mutate(
                    job_id, Stage.VALIDATED, "prepare_desktop", self.adapter.prepare
                )
                if resume:
                    self.store.update(job_id, stage=Stage.RECONCILING)
                reference = source.external_reference or ""
                # Reopening saved records also changes the active GUI editor.
                self.ui_started = True
                order, recovering_invoice = None, None
                if resume and prepared.get("defer_order_reopen_during_invoice_draft"):
                    checkpoint = self._verified_order_checkpoint(job_id)
                    if checkpoint is not None:
                        pending = self.adapter.current_invoice(checkpoint.number)
                        if (
                            pending is not None
                            and pending.relationship_evidence.get("editor_new")
                            and pending.relationship_evidence.get("editor_dirty")
                        ):
                            self._verify(source, checkpoint, persisted=True)
                            order, recovering_invoice = checkpoint, pending
                            self.store.log(
                                job_id,
                                Stage.RECONCILING,
                                "deferred_order_reopen",
                                "observed",
                                {
                                    "order_number": order.number,
                                    "invoice_number": pending.number,
                                    "reason": "Fakturama's unsaved follow-up editor retains the source Order identity. Reuse this job's verified saved checkpoint; reopen the Order after Invoice Save before success.",
                                },
                            )
                if order is None:
                    order = self._single_saved("Order", reference)
                invoice = self._single_saved("Invoice", reference)
                if invoice and not order:
                    raise ReviewRequired(
                        "ORPHAN_INVOICE", "Invoice exists without one matching saved Order"
                    )
                if order:
                    self._verify_generated_order(job_id, order)
                    self._verify(source, order, persisted=True)
                else:
                    self.ui_started = True
                    order = self.adapter.current_order(reference)
                    if order is None:
                        order = self._mutate(
                            job_id,
                            Stage.ORDER_OPEN,
                            "open_order",
                            lambda: self.adapter.open_order(source),
                        )
                    self._verify_generated_order(job_id, order)
                    self.store.transition(job_id, Stage.ORDER_OPEN, number=order.number)
                    self._debtor(job_id, source)
                    self.store.transition(job_id, Stage.DEBTOR_READY)
                    existing = self.adapter.read_order()
                    if len(existing.items) > len(source.items):
                        raise ReviewRequired(
                            "EXTRA_EDITOR_ROWS", "Unsaved editor has more rows than the source"
                        )
                    for index, item in enumerate(source.items):
                        self._product(job_id, item, index)
                    self.store.transition(job_id, Stage.ITEMS_READY)
                    order = self.adapter.read_order()
                    self._verify_generated_order(job_id, order)
                    self._verify(source, order, persisted=False)
                    self.adapter.evidence("order-before-save", output)
                    generated_order_number = order.number
                    self._mutate(
                        job_id,
                        Stage.ITEMS_READY,
                        "save_order",
                        self.adapter.save_order,
                        number=generated_order_number,
                    )
                    order = self._single_saved("Order", reference)
                    if order is None:
                        raise ReviewRequired(
                            "ORDER_SAVE_UNCONFIRMED", "Saved Order did not appear in Documents"
                        )
                    if order.number != generated_order_number:
                        raise ReviewRequired(
                            "ORDER_NUMBER_CHANGED",
                            "Saved Order number differs from its generated draft number",
                        )
                    self._verify(source, order, persisted=True)
                self.store.transition(job_id, Stage.ORDER_SAVED_VERIFIED, number=order.number)
                self.store.log(
                    job_id,
                    Stage.ORDER_SAVED_VERIFIED,
                    "verified_order",
                    "observed",
                    {"document": order.model_dump(mode="json")},
                )
                if invoice is None:
                    self.ui_started = True
                    invoice = recovering_invoice or self.adapter.current_invoice(order.number)
                    if invoice is None:
                        invoice = self._mutate(
                            job_id,
                            Stage.INVOICE_OPEN,
                            "follow_up_invoice",
                            lambda: self.adapter.follow_up_invoice(order),
                            order_number=order.number,
                        )
                    invoice = self._restore_lineage(job_id, invoice, order)
                    self._verify_generated_invoice(job_id, invoice)
                    if invoice.source_order_number != order.number:
                        raise ReviewRequired(
                            "INVOICE_LINK_MISMATCH",
                            "Follow-up Invoice does not refer to the source Order",
                        )
                    original = (invoice.number, invoice.document_date, invoice.service_date)
                    self.store.transition(
                        job_id,
                        Stage.INVOICE_OPEN,
                        number=invoice.number,
                        document_date=invoice.document_date.isoformat(),
                        service_date=invoice.service_date.isoformat(),
                    )
                    self._mutate(
                        job_id,
                        Stage.INVOICE_OPEN,
                        "set_payment",
                        lambda: self.adapter.set_payment(source),
                    )
                    invoice = self.adapter.read_invoice()
                    if original != (invoice.number, invoice.document_date, invoice.service_date):
                        raise ReviewRequired(
                            "GENERATED_FIELDS_CHANGED",
                            "Generated Invoice identifiers or dates changed",
                        )
                    self._verify(source, invoice, persisted=False)
                    self.adapter.evidence("invoice-before-save", output)
                    self._mutate(
                        job_id,
                        Stage.INVOICE_OPEN,
                        "save_invoice",
                        self.adapter.save_invoice,
                        number=invoice.number,
                    )
                    invoice = self._single_saved("Invoice", reference)
                    if invoice is None:
                        raise ReviewRequired(
                            "INVOICE_SAVE_UNCONFIRMED", "Saved Invoice did not appear in Documents"
                        )
                    if original != (invoice.number, invoice.document_date, invoice.service_date):
                        raise ReviewRequired(
                            "GENERATED_FIELDS_CHANGED",
                            "Saved Invoice changed its generated identifiers or dates",
                        )
                invoice = self._restore_lineage(job_id, invoice, order)
                self._verify_generated_invoice(job_id, invoice)
                self._verify(source, invoice, persisted=True)
                if invoice.source_order_number != order.number:
                    raise ReviewRequired(
                        "INVOICE_LINK_MISMATCH",
                        "Persisted Invoice lost its source Order relationship",
                    )
                final_order = self._single_saved("Order", reference)
                if final_order is None:
                    raise ReviewRequired(
                        "SOURCE_ORDER_MISSING", "Source Order disappeared during Invoice creation"
                    )
                self._verify_generated_order(job_id, final_order)
                self._verify(source, final_order, persisted=True)
                relationship = self.adapter.relationship(order.number, invoice.number, reference)
                invoice.relationship_evidence = {
                    **relationship,
                    "follow_up_observation": invoice.relationship_evidence,
                }
                self.store.log(
                    job_id,
                    Stage.INVOICE_OPEN,
                    "verified_saved_relationship",
                    "observed",
                    relationship,
                )
                write_json(
                    output / "observed-documents.json",
                    {
                        "order": final_order.model_dump(mode="json"),
                        "invoice": invoice.model_dump(mode="json"),
                        "mode": self.adapter.mode,
                    },
                )
                self.store.transition(job_id, Stage.INVOICE_SAVED_VERIFIED, number=invoice.number)
                evidence = self.adapter.evidence("final-verification", output)
                warnings, contact_evidence = self._source_value_warnings(
                    job_id, source, (final_order, invoice), output
                )
                pending_debtor = debtor_number = None
                for event in self.store.events(job_id):
                    if event["action"] == "select_debtor":
                        if event["status"] == "intent":
                            pending_debtor = event["data"].get("customer_id")
                        elif event["status"] == "observed":
                            debtor_number = pending_debtor
                receipt = CompletionReceipt(
                    job_id=job_id,
                    workspace=job["workspace"],
                    mode=job["mode"],
                    order_number=order.number,
                    invoice_number=invoice.number,
                    debtor_number=debtor_number,
                    order_date=final_order.order_date,
                    invoice_date=invoice.document_date,
                    invoice_service_date=invoice.service_date,
                    source_order_state=final_order.state,
                    relationship_evidence=invoice.relationship_evidence,
                    external_reference=reference,
                    currency=invoice.currency,
                    extraction_provenance={
                        "attempts": (job.get("extraction") or {}).get("attempts", []),
                        "source_revision": job["revision"],
                        "desktop_adapter": self.adapter.mode,
                    },
                    totals=invoice.totals,
                    payment_method=invoice.payment_method or "",
                    paid=invoice.paid,
                    payment_date=invoice.payment_date,
                    payment_value=invoice.payment_value,
                    evidence=[*evidence, *contact_evidence],
                    warnings=warnings,
                    source_field_agreement=not warnings,
                    build=source_identity(),
                )
                write_json(output / "receipt.json", receipt.model_dump(mode="json"))
                self.store.update(job_id, receipt=receipt.model_dump(mode="json"), review=None)
                self.store.transition(job_id, Stage.SUCCEEDED)
                return receipt
        except ReviewRequired as exc:
            self.store.update(
                job_id,
                stage=Stage.NEEDS_REVIEW,
                review={"code": exc.code, "message": str(exc), **exc.evidence},
            )
            self.store.log(
                job_id,
                Stage.NEEDS_REVIEW,
                "review_required",
                "observed",
                {"code": exc.code, "message": str(exc)},
            )
            return None
        except ConflictError:
            raise
        except Exception as exc:
            # An exception around a UI mutation is an unknown outcome, not permission to retry.
            self.store.update(
                job_id,
                stage=Stage.NEEDS_REVIEW if self.ui_started else Stage.FAILED,
                review={
                    "code": "UI_OUTCOME_UNKNOWN" if self.ui_started else "TECHNICAL_FAILURE",
                    "message": str(exc),
                    "exception": type(exc).__name__,
                },
            )
            return None

    def _debtor(self, job_id, source):
        candidates = self.adapter.debtor_candidates(source.customer)
        exact = [record for record in candidates if exact_debtor(record, source.customer)]
        if len(exact) > 1:
            raise ReviewRequired(
                "DEBTOR_AMBIGUOUS", "Debtor selector has duplicate or conflicting results"
            )
        if not exact and any(
            same_debtor_identity(record, source.customer) for record in candidates
        ):
            raise ReviewRequired(
                "DEBTOR_ADDRESS_CONFLICT",
                "An existing party has conflicting ZIP/City; review before creating or changing shared master data",
            )
        if not exact:
            self._mutate(
                job_id,
                Stage.ORDER_OPEN,
                "begin_debtor",
                lambda: self.adapter.begin_debtor(source.customer),
            )
            name = source.payment.method or ""
            payments = self.adapter.payment_candidates(name)
            if len(payments) > 1 or (payments and payments[0]["code"] != PAYMENT_CODES[name]):
                raise ReviewRequired(
                    "PAYMENT_CONFLICT",
                    "Exact payment method has duplicate or conflicting definitions",
                )
            if not payments:
                self._mutate(
                    job_id,
                    Stage.ORDER_OPEN,
                    "create_payment",
                    lambda: self.adapter.create_payment(name, PAYMENT_CODES[name]),
                    name=name,
                )
            self._mutate(
                job_id,
                Stage.ORDER_OPEN,
                "create_debtor",
                lambda: self.adapter.create_debtor(source.customer, name),
            )
            exact = [
                record
                for record in self.adapter.debtor_candidates(source.customer)
                if exact_debtor(record, source.customer)
            ]
            if len(exact) != 1:
                raise ReviewRequired(
                    "DEBTOR_SAVE_UNCONFIRMED",
                    "New Debtor could not be selected uniquely from the Order",
                )
        self._mutate(
            job_id,
            Stage.ORDER_OPEN,
            "select_debtor",
            lambda: self.adapter.select_debtor(exact[0]),
            customer_id=exact[0]["id"],
        )

    def _product(self, job_id, item, index):
        candidates = [
            p for p in self.adapter.product_candidates(item.sku or "") if p["sku"] == item.sku
        ]
        if len(candidates) > 1:
            raise ReviewRequired("PRODUCT_AMBIGUOUS", "Multiple exact SKU matches")
        if not candidates:
            rate = item.vat_percent or Decimal("0")
            name = f"VAT {format(rate.normalize(), 'f')}%"
            vats = self.adapter.vat_candidates(name)
            if len(vats) > 1 or (
                vats
                and (Decimal(vats[0]["rate"]) != rate or vats[0]["code"] != "S (Standard rate)")
            ):
                raise ReviewRequired(
                    "VAT_CONFLICT", "Required VAT definition conflicts or is duplicated"
                )
            if not vats:
                self._mutate(
                    job_id,
                    Stage.DEBTOR_READY,
                    "create_vat",
                    lambda: self.adapter.create_vat(name, rate),
                    name=name,
                )
            self._mutate(
                job_id,
                Stage.DEBTOR_READY,
                "create_product",
                lambda: self.adapter.create_product(item),
                sku=item.sku,
            )
            candidates = [
                p for p in self.adapter.product_candidates(item.sku or "") if p["sku"] == item.sku
            ]
            if len(candidates) != 1:
                raise ReviewRequired(
                    "PRODUCT_SAVE_UNCONFIRMED",
                    "New Product could not be selected uniquely from the Order",
                )
        product = candidates[0]
        if Decimal(product["vat_percent"]) != item.vat_percent:
            raise ReviewRequired(
                "PRODUCT_VAT_CONFLICT", "Existing SKU has a different VAT definition"
            )
        self._mutate(
            job_id,
            Stage.DEBTOR_READY,
            f"select_product:{index}",
            lambda: self.adapter.select_product(product, index),
            sku=item.sku,
        )
        self._mutate(
            job_id,
            Stage.DEBTOR_READY,
            f"set_line:{index}",
            lambda: self.adapter.set_line(index, item),
            sku=item.sku,
        )
