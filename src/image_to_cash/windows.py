"""Fakturama 2.2 Windows adapter, backed by current UI observations and NatTable copy."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .artifacts import write_json
from .config import Settings
from .models import (
    Address,
    Customer,
    ItemInput,
    ObservedDocument,
    OrderInput,
    ReviewRequired,
    Totals,
)
from .ui import SemanticUI, key
from .validation import email_exception_applies, flatten, normalize, reserved_email_domain


def parse_day(value: str) -> date:
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    raise ReviewRequired("APPLICATION_DATE_UNREADABLE", f"Cannot parse application date {value!r}")


def value(row: dict, *labels: str) -> str:
    matches = [
        row[label] for label in dict.fromkeys(key(label) for label in labels) if label in row
    ]
    if len(matches) != 1:
        raise ReviewRequired(
            "TABLE_COLUMN_UNAVAILABLE",
            f"Expected one observed column among {labels!r}; found {len(matches)}",
        )
    return matches[0]


def vat_percent(text: str) -> Decimal:
    raw = re.search(r"taxValue:\s*\[(-?\d+(?:\.\d+)?)\]", text)
    if raw:
        return Decimal(raw.group(1)) * 100
    printed = re.findall(r"(\d+(?:[.,]\d+)?)\s*%", text)
    if printed:
        return Decimal(printed[-1].replace(",", "."))
    raise ReviewRequired("VAT_RATE_UNREADABLE", "The observed VAT value has no explicit rate")


def document_state(raw: str) -> str:
    """Clipboard rows export each state's icon name; these are the names observed on screen."""
    return {
        "COMMAND_ORDER_PENDING": "open",
        "COMMAND_ORDER_COMPLETED": "completed",
        "COMMAND_CHECKED": "paid",
        "COMMAND_ERROR": "unpaid",
    }.get(raw, raw.strip().casefold())


def document_totals(subtotal: Decimal, vat: Decimal, gross: Decimal, discount: Decimal) -> Totals:
    # Fakturama's Total Net label is the item subtotal before document discount.
    # Total and VAT already include that discount; their difference is final net.
    return Totals(net=gross - vat if discount else subtotal, vat=vat, gross=gross)


class WindowsAdapter:
    mode = "live"
    METHODS = {
        "prepare",
        "saved",
        "current_order",
        "open_order",
        "debtor_candidates",
        "begin_debtor",
        "create_debtor",
        "select_debtor",
        "payment_candidates",
        "create_payment",
        "vat_candidates",
        "create_vat",
        "product_candidates",
        "create_product",
        "select_product",
        "set_line",
        "read_order",
        "save_order",
        "current_invoice",
        "follow_up_invoice",
        "set_payment",
        "read_invoice",
        "save_invoice",
        "relationship",
        "evidence",
        "discard_order_draft",
        "observe_saved_contact",
    }

    def __init__(self, settings: Settings, workspace: str, source: OrderInput | None = None):
        # The supported 2.2.0 validator rejects this reserved TLD even though it is
        # legitimate literal source data. Strict mode stops before creating GUI state;
        # the default continues and records what actually persisted as a typed warning.
        email = source.customer.email if source else None
        if settings.email_discrepancy_policy == "strict" and reserved_email_domain(email):
            raise ReviewRequired(
                "FAKTURAMA_EMAIL_REJECTED",
                "Fakturama 2.2.0 rejects the source's reserved .test email domain. The original source is unchanged; strict email policy requires review before desktop execution.",
            )

        def initialize():
            import pythoncom

            pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)

        self.executor = ThreadPoolExecutor(
            max_workers=1, initializer=initialize, thread_name_prefix="fakturama-uia"
        )
        try:
            self.driver = self.executor.submit(
                FakturamaDriver, settings, workspace, source
            ).result()
        except Exception:
            self.executor.shutdown(wait=True)
            raise

    def __getattr__(self, name):
        if name not in self.METHODS:
            raise AttributeError(name)

        def invoke(*args, **kwargs):
            return self.executor.submit(getattr(self.driver, name), *args, **kwargs).result()

        return invoke

    def close(self):
        self.executor.shutdown(wait=True)


class FakturamaDriver:
    def __init__(self, settings: Settings, workspace: str, source: OrderInput | None):
        self.ui = SemanticUI(settings, workspace)
        self.settings, self.workspace, self.source = settings, workspace, source
        self._customer: Customer | None = None
        self._source_order_number: str | None = None
        self._order_hint: str | None = None
        self._linked_invoice_number: str | None = None
        self._notices: list[str] = []

    def _dismiss_information(self):
        windows = self.ui.controls(self.ui.root(), "Window")
        dialogs = [window for window in windows if window.window_text() == "Information"]
        for dialog in dialogs:
            text = "\n".join(control.window_text() for control in self.ui.controls(dialog, "Text"))
            if not all(
                token in text
                for token in (
                    "has already ordered",
                    "Last time:",
                    "Invoice numbers:",
                    "Total volume:",
                )
            ):
                raise ReviewRequired(
                    "UNRECOGNIZED_INFORMATION",
                    "An unrecognized application notice requires review",
                    evidence={"message": text},
                )
            self.ui.click(dialog, "OK")
            self.ui.wait(
                lambda: [
                    window
                    for window in self.ui.controls(self.ui.root(), "Window")
                    if window.window_text() == "Information"
                ],
                lambda windows: not windows,
            )
            self._notices.append(text)
        return [window for window in windows if window.window_text() != "Information"]

    def prepare(self):
        self.ui.activate()
        dialogs = self._dismiss_information()
        cancelled_selectors = []
        for dialog in dialogs:
            if key(dialog.window_text()) not in {"selecttheaddress", "selectaproduct"}:
                raise ReviewRequired(
                    "UNRESOLVED_MODAL", "Reconcile the application dialog before resuming"
                )
            cancelled_selectors.append(dialog.window_text())
            self.ui.click(dialog, "Cancel")
        cleanup = self.ui.close_clean_editors()
        open_editors = [
            tab.window_text()
            for tab in self.ui.editor_stack().children(control_type="TabItem")
            if key(tab.window_text()) != "fakturama"
        ]
        self.ui.one(self.ui.root(), "File", "MenuItem").click_input()
        self.ui.one(self.ui.root(), "Preferences", "MenuItem").click_input()
        dialog = self.ui.dialog("Preferences")
        self.ui.one(dialog, "General", "TreeItem").click_input()
        original_locale = self.ui.get(dialog, "Currency locale", role="ComboBox")
        if original_locale != "Germany" and open_editors:
            self.ui.click(dialog, "Cancel")
            raise ReviewRequired(
                "CURRENCY_PROFILE_OPEN_EDITORS",
                "Close existing editors before changing the application's currency locale",
                evidence={"editors": open_editors, "locale": original_locale},
            )
        self.ui.combo(dialog, "Currency locale", "Germany")
        cash = self.ui.one(dialog, "use cash rounding", "CheckBox")
        original_cash = bool(cash.get_toggle_state())
        if original_cash:
            cash.click_input()
            self.ui.wait(lambda: cash.get_toggle_state(), lambda state: state == 0)
        original_scale = self.ui.get(dialog, "decimal places (currency)")
        if self.ui.decimal(original_scale) != 2:
            self.ui.set_number(dialog, "decimal places (currency)", Decimal("2"))
        self.ui.one(dialog, "Documents", "TreeItem").click_input()
        label = "immediately take over a clearly found item number"
        checkbox = self.ui.one(dialog, label, "CheckBox")
        changed = bool(checkbox.get_toggle_state())
        if changed:
            checkbox.click_input()
            self.ui.wait(lambda: checkbox.get_toggle_state(), lambda state: state == 0)
        applied = (
            changed
            or original_locale != "Germany"
            or original_cash
            or self.ui.decimal(original_scale) != 2
        )
        self.ui.click(dialog, "Apply and Close" if applied else "Cancel")
        self.ui.wait(
            lambda: self.ui.controls(self.ui.root(), "Window"), lambda windows: not windows
        )
        return {
            "preference": label,
            "original_enabled": changed,
            "configured_enabled": False,
            "reason": "Product searches must not insert rows before explicit selection",
            "currency_locale_before": original_locale,
            "currency_locale_configured": "Germany",
            "cash_rounding_before": original_cash,
            "currency_decimal_places": 2,
            "defer_order_reopen_during_invoice_draft": True,
            **cleanup,
            "cancelled_read_only_selectors": cancelled_selectors,
        }

    def _form(self, signature: set[str]):
        dialogs = self._dismiss_information()
        if dialogs:
            raise ReviewRequired(
                "UNRESOLVED_MODAL",
                "An application dialog must be reconciled before using an editor",
                evidence={"dialogs": [dialog.window_text() for dialog in dialogs]},
            )
        candidates = []
        for pane in self.ui.controls(self.ui.root(), "Pane"):
            if not pane.window_text():
                continue
            labels = {key(c.window_text()) for c in self.ui.controls(pane, "Text")}
            if {key(s) for s in signature}.issubset(labels):
                candidates.append(pane)
        if not candidates:
            raise ReviewRequired(
                "EDITOR_UNAVAILABLE", f"No active editor exposes fields {sorted(signature)}"
            )
        return min(candidates, key=lambda p: p.rectangle().width() * p.rectangle().height())

    def _doc(self):
        return self._form({"No.", "Cust.Ref.", "Items"})

    def _contact(self):
        return self._form({"Customer ID", "Company"})

    def _payment(self):
        return self._form({"Cash discount", "Net Days", "Description"})

    def _vat(self):
        return self._form({"Name", "Value", "Standard"})

    def _product(self):
        return self._form({"Item Number", "Price (gross)", "Stock"})

    def _draft(self, caption, observe, open_new, identity, expected):
        matches = [
            t
            for t in self.ui.editor_stack().children(control_type="TabItem")
            if key(t.window_text()) == key(caption)
        ]
        if len(matches) > 1:
            raise ReviewRequired(
                "DUPLICATE_DRAFT_EDITORS", f"Multiple {caption} editors require reconciliation"
            )
        if matches:
            matches[0].click_input()
        else:
            open_new()
        form = self.ui.wait(observe)
        if self.ui.get(form, identity) not in {"", expected}:
            raise ReviewRequired(
                "UNRELATED_MASTER_DRAFT", f"The open {caption} belongs to another source"
            )
        return form

    def _ensure_order_tab(self):
        try:
            pane = self._doc()
            if any(key(t.window_text()) == "order" for t in self.ui.controls(pane, "Text")):
                return pane
        except ReviewRequired:
            pass
        tabs = self.ui.editor_stack().children(control_type="TabItem")
        preferred = [
            t
            for t in tabs
            if "neworder" == key(t.window_text())
            or (self._order_hint and self._order_hint in t.window_text())
        ]
        for tab in preferred or tabs:
            tab.click_input()
            try:
                pane = self._doc()
                if any(key(t.window_text()) == "order" for t in self.ui.controls(pane, "Text")):
                    return pane
            except ReviewRequired:
                pass
        raise ReviewRequired("ORDER_EDITOR_LOST", "No surviving Order editor is available")

    def _ensure_debtor_tab(self):
        try:
            return self._contact()
        except ReviewRequired:
            pass
        tabs = self.ui.editor_stack().children(control_type="TabItem")
        company = self.source.customer.company if self.source else None
        preferred = [
            t
            for t in tabs
            if key(t.window_text()) == "newdebtor"
            or (company and normalize(company) in normalize(t.window_text()))
        ]
        for tab in preferred or tabs:
            tab.click_input()
            try:
                return self._contact()
            except ReviewRequired:
                pass
        raise ReviewRequired("DEBTOR_EDITOR_LOST", "No surviving Debtor editor is available")

    def _rows(self, navigation, query, *, document_kind=None):
        self.ui.restore_parts()
        pane = self.ui.navigate(navigation)
        if document_kind:
            # "This transaction" and customer nodes hide other saved records.
            # Select the global document-type root before searching references.
            category = self._document_category(pane, document_kind)
            if category is not None:
                category.click_input()
                self.ui.wait(category.is_selected)
        self.ui.search(pane, query)
        return pane, self.ui.rows(pane)

    def _document_category(self, pane, document_kind):
        """Fakturama adds a document-type node only once a document of that kind exists."""
        label = key(document_kind + "s")

        def nodes():
            return [c for c in self.ui.controls(pane, "TreeItem") if key(c.window_text()) == label]

        try:
            found = self.ui.wait(nodes, timeout=3)
        except ReviewRequired as exc:
            if exc.code != "UI_WAIT_TIMEOUT":
                raise
            # Without a node the search still covers the table, and saved() filters each
            # row by its icon, so an existing document of this kind cannot be overlooked.
            return None
        if len(found) != 1:
            raise ReviewRequired(
                "CONTROL_AMBIGUOUS",
                f"Expected one Documents node for {document_kind}; observed {len(found)}",
            )
        return found[0]

    def _selector(self, resource, title, query):
        pane = self._ensure_order_tab()
        self.ui.icon(pane, resource)
        dialog = self.ui.dialog(title)
        native = self.ui.native_control(dialog)
        import win32api

        left, top, right, bottom = win32api.GetMonitorInfo(
            win32api.MonitorFromWindow(self.ui.handle)
        )["Work"]
        width, height = round((right - left) * 0.95), round((bottom - top) * 0.75)
        native.move_window(
            x=left + (right - left - width) // 2,
            y=top + (bottom - top - height) // 2,
            width=width,
            height=height,
        )
        self.ui.search(dialog, query)
        return dialog, self.ui.rows(dialog)

    def current_order(self, reference):
        try:
            pane = self._ensure_order_tab()
        except ReviewRequired:
            return None
        if self.ui.get(pane, "Cust.Ref.") != reference:
            return None
        return (
            self.open_order(self.source) if self.source else self._read_document(pane, partial=True)
        )

    def open_order(self, source):
        self.source = source
        try:
            pane = self._ensure_order_tab()
            if self.ui.get(pane, "Cust.Ref.") not in {"", source.external_reference}:
                if self.ui.editor_stack().window_text().startswith("*"):
                    raise ReviewRequired(
                        "UNRELATED_ORDER_EDITOR", "An unrelated unsaved Order editor is open"
                    )
                self.ui.click(self.ui.root(), "Create: New Order")
                pane = self.ui.wait(
                    lambda: (
                        self._doc()
                        if key(self.ui.editor_stack().window_text()) == "neworder"
                        else None
                    )
                )
        except ReviewRequired as exc:
            if exc.code != "ORDER_EDITOR_LOST":
                raise
            self.ui.click(self.ui.root(), "Create: New Order")
            pane = self.ui.wait(self._doc)
        number = self.ui.get(pane, "No.")
        self._order_hint = number
        self.ui.set(pane, "Cust.Ref.", source.external_reference)
        self.ui.set_date(pane, "Date", source.order_date)
        price = [c for c in self.ui.controls(pane, "ComboBox") if not c.window_text()]
        if len(price) != 1:
            raise ReviewRequired(
                "PRICE_MODE_AMBIGUOUS", "Document Net/Gross mode could not be established"
            )
        self.ui.select_combo(price[0], "Net")
        self.ui.combo(pane, "VAT", "With VAT")
        self.ui.set_number(
            pane,
            "Discount",
            source.document_discount_percent or Decimal("0"),
            display_multiplier=-1,
        )
        if source.shipping_net:
            raise ReviewRequired(
                "SHIPPING_PROFILE_UNVERIFIED",
                "Non-zero shipping requires a verified shipping definition and VAT profile",
            )
        if self.ui.get(pane, "No.") != number:
            raise ReviewRequired(
                "ORDER_NUMBER_CHANGED", "Generated Order number changed during entry"
            )
        # An open Order has incomplete addresses/rows; its generated fields are still observable.
        return self._read_document(pane, partial=True)

    def debtor_candidates(self, source):
        dialog, rows = self._selector(
            "icons/20x20/contact_list.png",
            "Select the address",
            source.company or source.last_name or "",
        )
        result = []
        for row in rows:
            customer = Customer(
                company=value(row, "Company"),
                first_name=value(row, "First Name"),
                last_name=value(row, "Name", "Last Name"),
                billing=Address(postcode=value(row, "ZIP"), city=value(row, "City")),
            )
            result.append(
                {
                    "id": value(row, "No.", "No", "Number"),
                    "customer": customer.model_dump(mode="json"),
                    "row": row,
                }
            )
        self.ui.click(dialog, "Cancel")
        return result

    def begin_debtor(self, source):
        try:
            pane = self._ensure_debtor_tab()
            company = self.ui.get(pane, "Company")
            if company and normalize(company) != normalize(source.company or ""):
                if self.ui.editor_stack().window_text().lstrip().startswith("*"):
                    raise ReviewRequired(
                        "UNRELATED_DEBTOR_EDITOR", "A different unsaved Debtor editor is open"
                    )
                # A previously inspected, clean contact is not a draft for this
                # new customer. Close it without saving and open New Debtor.
                self.ui.close_saved_editor()
                nav = self.ui.pane("Navigation View")
                self.ui.one(nav, "New Contact", "Text").click_input()
                pane = self.ui.wait(self._contact)
        except ReviewRequired as exc:
            if exc.code != "DEBTOR_EDITOR_LOST":
                raise
            nav = self.ui.pane("Navigation View")
            self.ui.one(nav, "New Contact", "Text").click_input()
            pane = self.ui.wait(self._contact)
        self.ui.maximize_editor()
        pane = self._contact()
        self.ui.set(pane, "Company", source.company or "")
        self.ui.set(pane, "First Name Last Name", source.first_name or "", index=0)
        self.ui.set(pane, "First Name Last Name", source.last_name or "", index=1)
        self.ui.tab("Addresses", scope=pane)
        self.ui.tab("Main address", scope=pane)
        self._fill_address(source.billing, source, main=True)
        identical = source.billing == source.delivery
        self.ui.roles(
            pane, {"Invoice address", "Delivery address"} if identical else {"Invoice address"}
        )
        if not identical:
            additional = [
                t
                for t in self.ui.controls(pane, "TabItem")
                if "additional address" in t.window_text().casefold()
            ]
            if not additional:
                self.ui.click(self.ui.one(pane, "Main address", "Tab"), "+")
                additional = self.ui.wait(
                    lambda: [
                        t
                        for t in self.ui.controls(self._contact(), "TabItem")
                        if "additional address" in t.window_text().casefold()
                    ]
                )
            if len(additional) != 1:
                raise ReviewRequired(
                    "DELIVERY_ADDRESS_AMBIGUOUS",
                    "Expected one secondary delivery address for this source",
                )
            additional[0].click_input()
            self._fill_address(source.delivery, source, main=False)
            self.ui.roles(self._contact(), {"Delivery address"})
        self.ui.tab("Miscellaneous", scope=self._contact())
        pane = self._contact()
        self.ui.set(pane, "Alias name", source.alias or "")
        self.ui.set_number(pane, "Discount", Decimal("0"))
        self.ui.combo(pane, "Net or Gross", "Net")

    def _fill_address(self, address, customer, *, main):
        pane = self._contact()
        self.ui.set(
            pane,
            "additional name",
            address.additional_name or (address.recipient if not main else "") or "",
        )
        self.ui.set(pane, "Street", address.street or "")
        self.ui.set(pane, "ZIP - City", address.postcode or "", index=0)
        self.ui.set(pane, "ZIP - City", address.city or "", index=1)
        self.ui.combo(pane, "Country", address.country or "")
        if main:
            self.ui.set(pane, "E-Mail", customer.email or "")
            self.ui.set(pane, "Telephone", customer.telephone or "")

    def _read_customer(self):
        pane = self._contact()
        customer = Customer(
            company=self.ui.get(pane, "Company"),
            first_name=self.ui.get(pane, "First Name Last Name", index=0) or None,
            last_name=self.ui.get(pane, "First Name Last Name", index=1) or None,
        )
        self.ui.tab("Addresses", scope=pane)
        names = [
            tab.window_text()
            for tab in self.ui.controls(self._contact(), "TabItem")
            if tab.window_text() == "Main address"
            or "additional address" in tab.window_text().casefold()
        ]
        roles: dict[str, list[tuple[Address, str | None, str | None]]] = {
            "billing": [],
            "delivery": [],
        }
        for name in names:
            self.ui.tab(name, scope=self._contact())
            pane = self._contact()
            additional = self.ui.get(pane, "additional name") or None
            role_text = self.ui.get(pane, "address type")
            values = dict(
                street=self.ui.get(pane, "Street"),
                postcode=self.ui.get(pane, "ZIP - City", index=0),
                city=self.ui.get(pane, "ZIP - City", index=1),
                country=self.ui.get(pane, "Country", role="ComboBox"),
            )
            email, telephone = (
                self.ui.get(pane, "E-Mail") or None,
                self.ui.get(pane, "Telephone") or None,
            )
            for role, label in (("billing", "Invoice address"), ("delivery", "Delivery address")):
                if label not in role_text:
                    continue
                expected = getattr(self.source.customer, role) if self.source else None
                is_additional_line = bool(
                    expected
                    and expected.recipient == customer.company
                    and additional == expected.additional_name
                )
                address = Address(
                    recipient=customer.company
                    if is_additional_line
                    else additional or customer.company,
                    additional_name=additional if is_additional_line else None,
                    **values,
                )
                roles[role].append((address, email, telephone))
        if len(roles["billing"]) != 1 or len(roles["delivery"]) != 1:
            raise ReviewRequired(
                "CONTACT_ADDRESS_ROLES_AMBIGUOUS",
                "Expected one observed Invoice address and one Delivery address; tab order is not an address role",
            )
        customer.billing, customer.email, customer.telephone = roles["billing"][0]
        customer.delivery = roles["delivery"][0][0]
        self.ui.tab("Miscellaneous", scope=self._contact())
        customer.alias = self.ui.get(self._contact(), "Alias name") or None
        return customer

    @staticmethod
    def _verify_customer(expected, actual, email_policy="strict"):
        wanted, seen = flatten(expected), flatten(actual)
        exempt = {"source_customer_id"}
        if email_exception_applies(expected.email, email_policy):
            exempt.add("email")
        mismatches = [
            name
            for name, text in wanted.items()
            if text is not None
            and name not in exempt
            and normalize(str(text)) != normalize(str(seen.get(name) or ""))
        ]
        if mismatches:
            raise ReviewRequired(
                "CONTACT_VALUES_CONFLICT",
                "Observed contact values differ from the supplied source; Order execution stopped",
                evidence={"fields": mismatches},
            )

    def _close_contact_editors(self, customer):
        """A contact editor can show rejected text; only a reopened record shows saved values."""
        name = normalize(customer.company or customer.last_name or "")
        captions = [
            tab.window_text()
            for tab in self.ui.editor_stack().children(control_type="TabItem")
            if name and name in normalize(tab.window_text())
        ]
        for caption in captions:
            if caption.lstrip().startswith("*"):
                raise ReviewRequired(
                    "CONTACT_EDITOR_DIRTY",
                    "An unsaved contact editor must be reconciled before reading saved values",
                    evidence={"caption": caption},
                )
            matches = [
                tab
                for tab in self.ui.editor_stack().children(control_type="TabItem")
                if tab.window_text() == caption
            ]
            if len(matches) != 1:
                raise ReviewRequired(
                    "EDITOR_CLEANUP_AMBIGUOUS", "Contact editor names are not unique"
                )
            matches[0].click_input()
            self.ui.close_saved_editor()

    def _reopen_contact(self, number, customer):
        self._close_contact_editors(customer)
        pane, rows = self._rows("Debtors", customer.company or customer.last_name or "")
        matches = [i for i, row in enumerate(rows) if value(row, "No.", "No", "Number") == number]
        if len(matches) != 1:
            raise ReviewRequired(
                "DEBTOR_REOPEN_AMBIGUOUS", "Saved Debtor could not be reopened uniquely"
            )
        self.ui.open_row(pane, matches[0])
        self.ui.wait(self._contact)
        self.ui.maximize_editor()
        return self._read_customer()

    def observe_saved_contact(self, customer, destination):
        """Reopen the saved Debtor and record its persisted email with a screenshot."""
        self._close_contact_editors(customer)
        pane, rows = self._rows("Debtors", customer.company or customer.last_name or "")
        matching = self._matching_debtor_rows(rows, customer)
        if len(matching) != 1:
            raise ReviewRequired(
                "SAVED_CUSTOMER_AMBIGUOUS", "Cannot uniquely reopen the saved source Debtor"
            )
        self.ui.open_row(pane, matching[0])
        self.ui.wait(self._contact)
        self.ui.maximize_editor()
        fresh = self._read_customer()
        self._verify_customer(customer, fresh, self.settings.email_discrepancy_policy)
        self._customer = fresh
        pane = self._show_contact_role("Invoice address")
        if normalize(self.ui.get(pane, "E-Mail")) != normalize(fresh.email or ""):
            raise ReviewRequired(
                "CONTACT_EVIDENCE_MISMATCH",
                "The displayed E-Mail differs from the value read from the saved contact",
            )
        return {
            "mechanism": "reopened_saved_contact",
            "customer_id": self.ui.get(pane, "Customer ID"),
            "email": fresh.email,
            "telephone": fresh.telephone,
            "evidence": self.evidence("saved-contact", destination),
        }

    def _show_contact_role(self, role):
        """Display the address tab holding a role; a tab's selection can precede its repaint."""
        self.ui.tab("Addresses", scope=self._contact())
        names = [
            tab.window_text()
            for tab in self.ui.controls(self._contact(), "TabItem")
            if tab.window_text() == "Main address"
            or "additional address" in tab.window_text().casefold()
        ]
        for name in names:
            self.ui.tab(name, scope=self._contact())
            pane = self._contact()
            self.ui.settle(pane)
            if role in self.ui.get(pane, "address type"):
                return pane
        raise ReviewRequired("CONTACT_ROLE_UNAVAILABLE", f"No address tab holds the {role} role")

    @staticmethod
    def _matching_debtor_rows(rows, customer):
        return [
            index
            for index, row in enumerate(rows)
            if all(
                normalize(value(row, label)) == normalize(expected or "")
                for label, expected in (
                    ("Company", customer.company),
                    ("First Name", customer.first_name),
                    ("Name", customer.last_name),
                )
            )
        ]

    def create_debtor(self, source, payment_method):
        self._ensure_debtor_tab()
        self.ui.tab("Miscellaneous", scope=self._contact())
        pane = self._contact()
        choices = self.ui.native_control(
            self.ui.field(pane, "Payment", role="ComboBox")
        ).item_texts()
        if payment_method not in choices:
            # Contact editors snapshot payment choices at construction in 2.2.0.
            # Rebuild this unsaved owned draft only after the method is persisted.
            self.ui.discard_current_draft("New Debtor")
            self.begin_debtor(source)
            pane = self._contact()
        self.ui.combo(pane, "Payment", payment_method)
        number = self.ui.get(pane, "Customer ID")
        policy = self.settings.email_discrepancy_policy
        self._verify_customer(source, self._read_customer(), policy)
        self.ui.save()
        if self.ui.get(self._contact(), "Customer ID") != number:
            raise ReviewRequired(
                "CUSTOMER_ID_CHANGED", "Saved Customer ID differs from its generated draft value"
            )
        # This editor keeps displaying a rejected email after Save. Reopen the saved record.
        self._customer = self._reopen_contact(number, source)
        self._verify_customer(source, self._customer, policy)
        return {
            "id": number,
            "customer": self._customer.model_dump(mode="json"),
            "payment_method": payment_method,
        }

    def select_debtor(self, record):
        source = (
            self.source.customer if self.source else Customer.model_validate(record["customer"])
        )
        if self._customer is None:
            self._customer = self._reopen_contact(record["id"], source)
        self._verify_customer(source, self._customer, self.settings.email_discrepancy_policy)
        self.ui.tab("Invoice address", scope=self._ensure_order_tab())
        dialog, rows = self._selector(
            "icons/20x20/contact_list.png",
            "Select the address",
            source.company or source.last_name or "",
        )
        matches = [
            i for i, row in enumerate(rows) if value(row, "No.", "No", "Number") == record["id"]
        ]
        if len(matches) != 1:
            self.ui.click(dialog, "Cancel")
            raise ReviewRequired(
                "DEBTOR_SELECTION_AMBIGUOUS", "Saved Debtor was not an exact unique selector row"
            )
        self.ui.select_row(dialog, matches[0])
        self.ui.click(dialog, "OK")
        # 2.2.0 binds a newly added secondary address using the selected tab's
        # index. Select the same verified contact from the Delivery tab once so
        # both document-address bindings exist before editing their copies.
        # No master is changed or saved by either selector action.
        self.ui.tab("Delivery address", scope=self._doc())
        dialog, rows = self._selector(
            "icons/20x20/contact_list.png",
            "Select the address",
            source.company or source.last_name or "",
        )
        matches = [
            index
            for index, row in enumerate(rows)
            if value(row, "No.", "No", "Number") == record["id"]
        ]
        if len(matches) != 1:
            self.ui.click(dialog, "Cancel")
            raise ReviewRequired(
                "DEBTOR_SELECTION_AMBIGUOUS",
                "Cannot initialize the second address binding with the same unique saved Debtor",
            )
        self.ui.select_row(dialog, matches[0])
        self.ui.click(dialog, "OK")
        pane = self._doc()
        # Document addresses are editable copies. Render the verified master fields
        # explicitly so an automatic contact-name line does not alter source addresses.
        for label, address in (
            ("Invoice address", self._customer.billing),
            ("Delivery address", self._customer.delivery),
        ):
            tabs = [
                t for t in self.ui.controls(pane, "TabItem") if key(t.window_text()) == key(label)
            ]
            if len(tabs) != 1:
                raise ReviewRequired("DOCUMENT_ADDRESS_TAB_MISSING", f"{label} tab is unavailable")
            tabs[0].click_input()
            group = next(
                t for t in self.ui.controls(pane, "Tab") if key(t.window_text()) == key(label)
            )
            edits = self.ui.controls(group, "Edit")
            if len(edits) != 1:
                raise ReviewRequired(
                    "DOCUMENT_ADDRESS_AMBIGUOUS", "Address copy input is not unique"
                )
            text = "\n".join(
                v
                for v in [
                    address.recipient,
                    address.additional_name,
                    address.street,
                    f"{address.postcode} {address.city}",
                    address.country,
                ]
                if v
            )
            self.ui.native_control(edits[0]).set_edit_text(text.replace("\n", "\r\n"))
            self.ui.field(pane, "Cust.Ref.").set_focus()
            if normalize(self.ui.value(edits[0])) != normalize(text):
                raise ReviewRequired(
                    "DOCUMENT_ADDRESS_UNCONFIRMED",
                    "Document address copy did not retain the verified values",
                )
        if self.source:
            self.ui.set_number(
                pane,
                "Discount",
                self.source.document_discount_percent or Decimal("0"),
                display_multiplier=-1,
            )

    def payment_candidates(self, name):
        pane, rows = self._rows("terms of payment", name)
        exact = [(i, row) for i, row in enumerate(rows) if value(row, "Name") == name]
        result = []
        for i, _row in exact:
            self.ui.open_row(pane, i)
            form = self.ui.wait(self._payment)
            code = self.ui.get(form, "!editorPaymentPaymentcode!", role="ComboBox").strip()
            result.append({"id": name, "name": name, "code": code})
        return result

    def create_payment(self, name, code):
        try:
            pane = self._payment()
        except ReviewRequired:
            self._rows("terms of payment", name)
            self.ui.click(self.ui.root(), "Create a new term of payment")
            pane = self.ui.wait(self._payment)
        self.ui.set(pane, "Name", name)
        self.ui.set(pane, "Description", name)
        self.ui.combo(pane, "!editorPaymentPaymentcode!", code)
        for field in ("Cash discount", "Discount Days", "Net Days"):
            self.ui.set_number(pane, field, Decimal("0"))
        for field in ("Text 'unpaid'", "Text 'deposit'", "Text 'paid'"):
            self.ui.set(pane, field, "")
        if self.ui.get(pane, "Account", role="ComboBox"):
            raise ReviewRequired("PAYMENT_ACCOUNT_NOT_BLANK", "New payment Account is not blank")
        self.ui.save()
        self._ensure_debtor_tab()
        return {"id": name, "name": name, "code": code}

    def vat_candidates(self, name):
        pane, rows = self._rows("VATs", name)
        result = []
        for index, row in enumerate(rows):
            if value(row, "Name") != name:
                continue
            self.ui.open_row(pane, index)
            form = self.ui.wait(self._vat)
            code_labels = [
                c.window_text()
                for c in self.ui.controls(form, "ComboBox")
                if "code" in c.window_text().casefold()
            ]
            if len(code_labels) != 1:
                raise ReviewRequired(
                    "VAT_CODE_UNAVAILABLE", "VAT E-Invoice code control is ambiguous"
                )
            result.append(
                {
                    "id": name,
                    "name": name,
                    "rate": str(self.ui.decimal(self.ui.get(form, "Value"))),
                    "code": self.ui.get(form, code_labels[0], role="ComboBox").strip(),
                }
            )
        return result

    def create_vat(self, name, rate):
        self.ui.restore_parts()
        self.ui.navigate("VATs")
        try:
            form = self._vat()
            if self.ui.get(form, "Name") not in {"", name}:
                raise ReviewRequired(
                    "UNRELATED_VAT_EDITOR", "A different unsaved VAT editor is active"
                )
        except ReviewRequired as exc:
            if exc.code != "EDITOR_UNAVAILABLE":
                raise
            self.ui.click(self.ui.root(), "Create a new tax rate")
            form = self.ui.wait(self._vat)
        self.ui.set(form, "Name", name)
        self.ui.set(form, "Description", name)
        self.ui.set_number(form, "Value", rate)
        code_labels = [
            c.window_text()
            for c in self.ui.controls(form, "ComboBox")
            if "code" in c.window_text().casefold()
        ]
        if len(code_labels) != 1:
            raise ReviewRequired("VAT_CODE_UNAVAILABLE", "Cannot establish the E-Invoice VAT code")
        self.ui.combo(form, code_labels[0], "S (Standard rate)")
        self.ui.save()
        return {"id": name, "name": name, "rate": str(rate), "code": "S (Standard rate)"}

    def product_candidates(self, sku):
        dialog, rows = self._selector("icons/20x20/product_list.png", "Select a product", sku)
        result = []
        for row in rows:
            number = value(row, "Item Number", "Item No.")
            if number != sku:
                continue
            rate = vat_percent(value(row, "VAT"))
            result.append(
                {
                    "id": number,
                    "sku": number,
                    "vat_percent": str(rate),
                    "row": row,
                }
            )
        self.ui.click(dialog, "Cancel")
        return result

    def create_product(self, item):
        from .validation import master_gross

        nav = self.ui.pane("Navigation View")
        form = self._draft(
            "New Product",
            self._product,
            lambda: self.ui.one(nav, "New product", "Text").click_input(),
            "Item Number",
            item.sku,
        )
        self.ui.maximize_editor()
        form = self._product()
        self.ui.set(form, "Item Number", item.sku)
        self.ui.set(form, "Name", item.description)
        self.ui.set(form, "Description", item.description)
        vat_name = f"VAT {format(item.vat_percent.normalize(), 'f')}%"
        self.ui.combo(form, "VAT", vat_name)
        self.ui.set_number(form, "Price (gross)", master_gross(item))
        self.ui.set_number(form, "cost price (net)", Decimal("0"))
        # A new SWT numeric field displays 0 for an unset model value. A real
        # change in this unsaved draft ensures Stock is assigned zero, rather
        # than leaving the exported persisted value null behind a formatted 0.
        self.ui.set_number(form, "Stock", Decimal("1"))
        self.ui.set_number(form, "Stock", Decimal("0"))
        self.ui.save()
        form = self._product()
        expected = {
            "Item Number": item.sku,
            "Name": item.description,
            "Description": item.description,
        }
        for label, text in expected.items():
            if normalize(self.ui.get(form, label)) != normalize(text):
                raise ReviewRequired(
                    "PRODUCT_SAVE_UNCONFIRMED", f"Saved Product {label} differs from the source"
                )
        for label, amount in (
            ("Price (gross)", master_gross(item)),
            ("cost price (net)", Decimal("0")),
            ("Stock", Decimal("0")),
        ):
            if self.ui.decimal(self.ui.get(form, label)) != amount:
                raise ReviewRequired(
                    "PRODUCT_SAVE_UNCONFIRMED",
                    f"Saved Product {label} differs from the required value",
                )
        self.ui.close_saved_editor()
        return {"id": item.sku, "sku": item.sku, "vat_percent": str(item.vat_percent)}

    def select_product(self, record, index):
        pane = self._ensure_order_tab()
        existing = self.ui.rows(pane)
        if len(existing) > index:
            if value(existing[index], "Item No.", "Item Number") != record["sku"]:
                raise ReviewRequired(
                    "ROW_IDENTITY_CONFLICT",
                    "The surviving Order row has a different SKU; no transaction values were changed",
                )
            return
        dialog, rows = self._selector(
            "icons/20x20/product_list.png", "Select a product", record["sku"]
        )
        matches = [
            i
            for i, row in enumerate(rows)
            if value(row, "Item Number", "Item No.") == record["sku"]
        ]
        if len(matches) != 1:
            self.ui.click(dialog, "Cancel")
            raise ReviewRequired(
                "PRODUCT_SELECTION_AMBIGUOUS", "Exact saved SKU did not appear uniquely"
            )
        self.ui.select_row(dialog, matches[0])
        self.ui.click(dialog, "OK")

    def set_line(self, index, item):
        pane = self._ensure_order_tab()
        self.ui.maximize_editor()
        pane = self._doc()
        headers, _ = self.ui.copy_rows(pane)
        labels = {key(h): h for h in headers}
        for aliases, amount in [
            (("Qty.", "Qty"), item.quantity),
            (("U.Price",), item.unit_net),
            (("Discount",), item.discount_percent),
            (("Name",), item.description),
            (("Description",), item.description),
        ]:
            choices = [labels[key(alias)] for alias in aliases if key(alias) in labels]
            if len(set(choices)) != 1:
                raise ReviewRequired(
                    "LINE_COLUMN_UNAVAILABLE", f"Could not establish item column {aliases}"
                )
            self.ui.cell(pane, index, choices[0], amount)

    def _lines(self, pane, rows=None):
        result = []
        for row in self.ui.rows(pane) if rows is None else rows:
            sku = value(row, "Item No.", "Item Number")
            if not sku:
                continue
            rate = vat_percent(value(row, "VAT"))
            result.append(
                ItemInput(
                    sku=sku,
                    description=value(row, "Description"),
                    quantity=self.ui.decimal(value(row, "Qty.", "Qty")),
                    unit_net=self.ui.decimal(value(row, "U.Price")),
                    discount_percent=self.ui.decimal(value(row, "Discount")) * -100,
                    vat_percent=rate,
                    printed_net=self.ui.decimal(value(row, "Price")),
                )
            )
        return result

    def _address_copy(self, pane, label):
        self.ui.tab(label, scope=pane)
        tab = self.ui.one(pane, label, "Tab")
        fields = self.ui.controls(tab, "Edit")
        if len(fields) != 1:
            raise ReviewRequired("DOCUMENT_ADDRESS_UNREADABLE", "Expected one address text input")
        lines = [s.strip() for s in self.ui.value(fields[0]).splitlines() if s.strip()]
        postal = next((i for i, line in enumerate(lines) if re.match(r"^\d{4,10}\s+", line)), None)
        if postal is None or postal < 2 or postal + 1 >= len(lines):
            raise ReviewRequired(
                "DOCUMENT_ADDRESS_UNREADABLE", "Cannot reconcile the current address text"
            )
        postcode, city = lines[postal].split(maxsplit=1)
        return Address(
            recipient=lines[0],
            additional_name=" ".join(lines[1 : postal - 1]) or None,
            street=lines[postal - 1],
            postcode=postcode,
            city=city,
            country=lines[postal + 1],
        )

    def _shipping_amount(self, pane):
        anchor = self.ui.field(pane, "Shipping", role="ComboBox").rectangle()
        choices = [
            edit
            for edit in self.ui.controls(pane, "Edit")
            if edit.rectangle().left >= anchor.right
            and min(edit.rectangle().bottom, anchor.bottom) - max(edit.rectangle().top, anchor.top)
            > min(edit.rectangle().height(), anchor.height()) * 0.35
        ]
        if len(choices) != 1:
            raise ReviewRequired(
                "SHIPPING_VALUE_UNREADABLE",
                "Cannot identify the observed shipping amount beside its selector",
            )
        return self.ui.decimal(self.ui.value(choices[0]))

    def _read_document(self, pane, *, persisted=False, partial=False):
        kind = (
            "Invoice"
            if any(key(t.window_text()) == "invoice" for t in self.ui.controls(pane, "Text"))
            else "Order"
        )
        document_date = parse_day(self.ui.get(pane, "Date"))
        if partial:
            customer, lines = Customer(), []
            raw_rows = []
        else:
            if self._customer is None:
                raise ReviewRequired(
                    "CUSTOMER_OBSERVATION_MISSING",
                    "Reopen and verify the linked Debtor before accepting document fields",
                )
            customer = self._customer.model_copy(deep=True)
            customer.billing = self._address_copy(pane, "Invoice address")
            customer.delivery = self._address_copy(pane, "Delivery address")
            raw_rows = self.ui.rows(pane)
            lines = self._lines(pane, raw_rows)
        currencies = []
        for row in raw_rows:
            for label in ("U.Price", "Price"):
                match = re.match(r"^([A-Z]{3})\s", value(row, label))
                if not match:
                    raise ReviewRequired(
                        "CURRENCY_UNREADABLE",
                        "A monetary table cell does not expose its currency code",
                    )
                currencies.append(match.group(1))
        if len(set(currencies)) > 1:
            raise ReviewRequired(
                "CURRENCY_CONFLICT", "The saved document contains conflicting currency codes"
            )
        net_labels = [
            label
            for label in ("Total Net", "Total Gross")
            if any(t.window_text() == label for t in self.ui.controls(pane, "Text"))
        ]
        if len(net_labels) != 1:
            raise ReviewRequired(
                "DOCUMENT_TOTAL_UNREADABLE", "Cannot establish net/gross total field"
            )
        subtotal = self.ui.decimal(self.ui.get(pane, net_labels[0]))
        document_discount = -self.ui.decimal(self.ui.get(pane, "Discount"))
        totals = document_totals(
            subtotal,
            self.ui.decimal(self.ui.get(pane, "VAT")),
            self.ui.decimal(self.ui.get(pane, "Total")),
            document_discount,
        )
        mode = "Net" if net_labels[0] == "Total Net" else "Gross"
        service_date = (
            parse_day(self.ui.get(pane, "Service date")) if kind == "Invoice" else document_date
        )
        order_date = (
            parse_day(self.ui.get(pane, "Order Date")) if kind == "Invoice" else document_date
        )
        observed = ObservedDocument(
            number=self.ui.get(pane, "No."),
            kind=kind,
            external_reference=self.ui.get(pane, "Cust.Ref."),
            currency=currencies[0] if currencies else None,
            displayed_item_subtotal=subtotal,
            net_is_derived=bool(document_discount),
            order_date=order_date,
            document_date=document_date,
            service_date=service_date,
            price_mode=mode,
            vat_mode=self.ui.get(pane, "VAT", role="ComboBox"),
            customer=customer,
            items=lines,
            totals=totals,
            document_discount_percent=document_discount,
            shipping_net=self._shipping_amount(pane),
            source_order_number=self._source_order_number
            if kind == "Invoice" and self.ui.get(pane, "No.") == self._linked_invoice_number
            else None,
            relationship_evidence={
                "mechanism": "observed_fakturama_follow_up_action",
                "order_number": self._source_order_number,
                "invoice_number": self._linked_invoice_number,
            }
            if kind == "Invoice" and self.ui.get(pane, "No.") == self._linked_invoice_number
            else {},
            application_notices=list(self._notices),
            persisted=persisted,
        )
        if kind == "Invoice":
            if partial:
                caption = self.ui.editor_stack().window_text()
                observed.relationship_evidence.update(
                    editor_caption=caption,
                    editor_dirty=caption.startswith("*"),
                    editor_new=key(caption) == "newinvoice",
                )
            paid = self.ui.one(pane, "paid", "CheckBox")
            observed.paid = bool(paid.get_toggle_state())
            observed.payment_method = self.ui.get(pane, "paid", role="ComboBox")
            if observed.paid:
                observed.payment_date = parse_day(self.ui.get(pane, "at"))
                observed.payment_value = self.ui.decimal(self.ui.get(pane, "Value"))
            observed.state = "paid" if observed.paid else "open"
        return observed

    def read_order(self):
        self._ensure_order_tab()
        self.ui.maximize_editor()
        return self._read_document(self._doc())

    def save_order(self):
        self._ensure_order_tab()
        self.ui.save()

    def discard_order_draft(self, number, reference):
        """Discard only the positively identified, never-saved Order editor."""
        self._order_hint = number
        try:
            pane = self._ensure_order_tab()
        except ReviewRequired as exc:
            if exc.code != "ORDER_EDITOR_LOST":
                raise
            return {"number": number, "external_reference": reference, "draft_present": False}
        identity = {
            "caption": self.ui.editor_stack().window_text(),
            "number": self.ui.get(pane, "No."),
            "external_reference": self.ui.get(pane, "Cust.Ref."),
        }
        if (
            key(identity["caption"]) != "neworder"
            or identity["number"] != number
            or identity["external_reference"] != reference
        ):
            raise ReviewRequired(
                "DRAFT_IDENTITY_UNCONFIRMED",
                "The open Order editor is not the job's unsaved draft",
                evidence=identity,
            )
        self.ui.discard_current_draft("New Order")
        return {**identity, "draft_present": True}

    def current_invoice(self, order_number):
        found = []
        for tab in self.ui.editor_stack().children(control_type="TabItem"):
            tab.click_input()
            try:
                pane = self._doc()
            except ReviewRequired as exc:
                if exc.code == "EDITOR_UNAVAILABLE":
                    continue
                raise
            if any(key(t.window_text()) == "invoice" for t in self.ui.controls(pane, "Text")):
                if self.source and self.ui.get(pane, "Cust.Ref.") != self.source.external_reference:
                    if tab.window_text().startswith("*"):
                        raise ReviewRequired(
                            "UNRELATED_INVOICE_EDITOR",
                            "An unsaved Invoice editor belongs to a different source reference",
                        )
                    continue
                found.append((tab, self._read_document(pane, partial=True)))
        if len(found) > 1:
            raise ReviewRequired(
                "DUPLICATE_INVOICE_EDITORS", "Multiple Invoice drafts require reconciliation"
            )
        if not found:
            return None
        found[0][0].click_input()
        return found[0][1]

    def follow_up_invoice(self, order):
        self._ensure_order_tab()
        self.ui.maximize_editor()
        pane = self._doc()
        observed_parent = self.ui.get(pane, "No.")
        if (
            observed_parent != order.number
            or self.ui.get(pane, "Cust.Ref.") != order.external_reference
            or self.ui.editor_stack().window_text().startswith("*")
        ):
            raise ReviewRequired(
                "FOLLOW_UP_PARENT_UNCONFIRMED", "Follow-up requires the observed saved source Order"
            )
        group = self.ui.one(pane, "Create a follow-up document", "Group")
        self.ui.click(group, "Invoice")
        self._source_order_number = observed_parent
        self.ui.wait(
            lambda: any(
                key(t.window_text()) == "invoice" for t in self.ui.controls(self._doc(), "Text")
            )
        )
        self._linked_invoice_number = self.ui.get(self._doc(), "No.")
        # Journal the observed generated identity immediately. Full line/payment
        # verification follows separately, before Save, and must not erase this
        # useful checkpoint if a later control cannot be read.
        return self._read_document(self._doc(), partial=True)

    def set_payment(self, source):
        pane = self._doc()
        self.ui.combo(pane, "paid", source.payment.method)
        check = self.ui.one(pane, "paid", "CheckBox")
        paid = normalize(source.payment.status or "").upper() == "PAID"
        if bool(check.get_toggle_state()) != paid:
            check.click_input()
        if paid:
            self.ui.set_date(pane, "at", source.payment.payment_date)
            self.ui.set_number(pane, "Value", source.totals.gross)

    def read_invoice(self):
        self.ui.maximize_editor()
        if self._customer is None:
            self._observe_source_customer(self.ui.get(self._doc(), "No."))
            self.ui.maximize_editor()
        return self._read_document(self._doc())

    def save_invoice(self):
        self.ui.save()

    def relationship(self, order_number, invoice_number, reference):
        pane = self._doc()
        if self.ui.get(pane, "No.") != order_number or self.ui.get(pane, "Cust.Ref.") != reference:
            raise ReviewRequired(
                "RELATIONSHIP_ANCHOR_MISMATCH",
                "Reopen the verified source Order before inspecting its transaction",
            )
        self.ui.restore_parts()
        pane = self.ui.navigate("Documents")
        category = self.ui.one(pane, "This transaction", "TreeItem")
        category.click_input()
        self.ui.wait(category.is_selected)
        # An empty search proves application grouping independently of the ref.
        self.ui.search(pane, "")
        rows = self.ui.rows(pane)
        pair = [row for row in rows if value(row, "Document") in {order_number, invoice_number}]
        if (
            len(pair) != 2
            or {value(row, "Document") for row in pair} != {order_number, invoice_number}
            or any(value(row, "Cust.Ref.") != reference for row in pair)
        ):
            raise ReviewRequired(
                "SAVED_RELATIONSHIP_UNCONFIRMED",
                "Saved Order and Invoice do not appear together in Fakturama's unfiltered This transaction group",
                evidence={"rows": rows},
            )
        return {
            "mechanism": "saved_fakturama_transaction_group",
            "scope": "This transaction",
            "search": "",
            "order_number": order_number,
            "invoice_number": invoice_number,
            "observed_documents": rows,
        }

    def saved(self, kind, reference):
        pane, rows = self._rows("Documents", reference, document_kind=kind)
        if len(rows) > 2:
            raise ReviewRequired(
                "DUPLICATE_DOCUMENTS", "More than two saved documents share this source reference"
            )
        result = []
        for row in rows:
            if value(row, "Cust.Ref.") != reference:
                continue
            icon = row.get("column0")
            if (kind == "Invoice" and icon == "ICON_ORDER") or (
                kind == "Order" and icon == "ICON_INVOICE"
            ):
                continue
            number = value(row, "Document")
            # An editor with no dirty marker may still be a cached object. Close
            # it and open the observed persisted row again before claiming Save.
            tabs = [
                tab
                for tab in self.ui.editor_stack().children(control_type="TabItem")
                if tab.window_text().lstrip("*") == number
            ]
            if len(tabs) > 1:
                raise ReviewRequired(
                    "DUPLICATE_DOCUMENT_EDITORS",
                    "Multiple editors expose one saved document number",
                )
            if tabs:
                tabs[0].click_input()
                self.ui.close_saved_editor()
            pane, fresh_rows = self._rows("Documents", reference, document_kind=kind)
            matching = [
                index
                for index, candidate in enumerate(fresh_rows)
                if value(candidate, "Document") == number
                and value(candidate, "Cust.Ref.") == reference
            ]
            if len(matching) != 1:
                raise ReviewRequired(
                    "SAVED_DOCUMENT_REOPEN_AMBIGUOUS",
                    "The saved document row changed during reconciliation",
                )
            self.ui.open_row(pane, matching[0], column="Document")

            def expected_editor(expected_number=number):
                self._dismiss_information()
                candidate = self._doc()
                return candidate if self.ui.get(candidate, "No.") == expected_number else None

            editor = self.ui.wait(expected_editor)
            self.ui.maximize_editor()
            editor = self._doc()
            if self._customer is None:
                self._observe_source_customer(number)
                self.ui.maximize_editor()
                editor = self._doc()
            observed = self._read_document(editor, persisted=True)
            if observed.kind == kind:
                observed.state = document_state(value(row, "State"))
                observed.documents_row = dict(row)
                result.append(observed)
        return result

    def _observe_source_customer(self, document_number):
        if self.source is None:
            raise ReviewRequired(
                "SOURCE_MISSING", "Saved-document recovery requires the validated source"
            )
        customer = self.source.customer
        original_caption = self.ui.editor_stack().window_text()
        self._close_contact_editors(customer)
        pane, rows = self._rows("Debtors", customer.company or customer.last_name or "")
        matching = self._matching_debtor_rows(rows, customer)
        if len(matching) != 1:
            raise ReviewRequired(
                "SAVED_CUSTOMER_AMBIGUOUS",
                "Cannot uniquely re-observe the source Debtor during saved-document recovery",
            )
        self.ui.open_row(pane, matching[0])
        self.ui.wait(self._contact)
        self._customer = self._read_customer()
        self._verify_customer(customer, self._customer, self.settings.email_discrepancy_policy)
        tabs = [
            tab
            for tab in self.ui.editor_stack().children(control_type="TabItem")
            if tab.window_text() == original_caption
        ]
        if len(tabs) != 1:
            raise ReviewRequired(
                "DOCUMENT_EDITOR_AMBIGUOUS", "Cannot return to the reopened persisted document"
            )
        tabs[0].click_input()
        if self.ui.get(self._doc(), "No.") != document_number:
            raise ReviewRequired(
                "DOCUMENT_EDITOR_MISMATCH",
                "Returning from source Debtor observation selected a different document",
            )

    def evidence(self, label, destination):
        self.ui.activate()
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / f"{label}.png"
        self.ui.root().capture_as_image().save(path)
        return [path.name]


def inspect_application(output: Path) -> dict:
    from pywinauto import Desktop

    output.mkdir(parents=True, exist_ok=True)
    windows = [
        w for w in Desktop(backend="uia").windows() if w.window_text().startswith("Fakturama")
    ]
    records = []
    for index, root in enumerate(windows):
        controls = []
        for control in [root, *root.descendants()]:
            info = control.element_info
            rect = control.rectangle()
            controls.append(
                {
                    "name": info.name,
                    "role": info.control_type,
                    "class": info.class_name,
                    "rectangle": [rect.left, rect.top, rect.right, rect.bottom],
                    "visible": control.is_visible(),
                }
            )
        write_json(output / f"window-{index}.json", controls)
        root.capture_as_image().save(output / f"window-{index}.png")
        records.append({"window": root.window_text(), "controls": len(controls)})
    return {"windows": records, "output": str(output)}
