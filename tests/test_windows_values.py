"""Regression cases from real SWT/clipboard observations; no GUI required."""

import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from image_to_cash.doctor import workspace_matches_title
from image_to_cash.models import ReviewRequired
from image_to_cash.ui import SemanticUI, clipboard_access, key
from image_to_cash.windows import (
    FakturamaDriver,
    WindowsAdapter,
    document_state,
    document_totals,
    value,
    vat_percent,
)


@pytest.mark.parametrize(
    "raw,state",
    [
        ("COMMAND_ORDER_PENDING", "open"),
        ("COMMAND_CHECKED", "paid"),
        ("COMMAND_ERROR", "unpaid"),
        ("Something New", "something new"),
    ],
)
def test_exported_state_icons_map_to_the_state_shown_on_screen(raw, state):
    assert document_state(raw) == state


def test_punctuation_only_controls_remain_distinct():
    assert key("+") != key("")
    assert key("No.") == key("No")
    assert value({"no": "CUST000001"}, "No.", "No", "Number") == "CUST000001"


def test_vat_uses_exported_rate_even_when_display_name_is_wrong():
    assert vat_percent("VAT taxValue: [0.07] name: [VAT 19%]") == Decimal("7")
    assert vat_percent("Standard rate (19,0 %)") == Decimal("19")
    with pytest.raises(ReviewRequired):
        vat_percent("Standard rate")


def test_known_application_locale_accepts_fractional_quantities():
    assert SemanticUI.decimal("1,005") == Decimal("1.005")
    assert SemanticUI.decimal("1.234,56 €") == Decimal("1234.56")
    assert SemanticUI.decimal("EUR 1234.56") == Decimal("1234.56")


@pytest.mark.parametrize("inline_editor", [False, True])
def test_invoice_table_detection_excludes_remarks_container(inline_editor):
    def control(width, height, children=(), class_name="SWT_Window0"):
        return SimpleNamespace(
            rectangle=lambda: SimpleNamespace(width=lambda: width, height=lambda: height),
            class_name=lambda: class_name,
            is_visible=lambda: True,
            children=lambda: children,
        )

    edit = control(120, 25, class_name="Edit")
    table = control(1400, 377, [edit] if inline_editor else [])
    remarks = control(774, 65, [control(770, 61, class_name="Edit")])
    scope = control(1500, 1000)
    ui = SemanticUI.__new__(SemanticUI)
    ui.activate = Mock()
    ui.native_control = Mock(return_value=SimpleNamespace(descendants=lambda: [table, remarks]))
    assert ui.table(scope) is table


@pytest.mark.parametrize("known", [True, False])
def test_only_known_order_history_information_can_be_dismissed(known):
    root, dialog = object(), Mock(window_text=lambda: "Information")
    message = (
        "Customer has already ordered 1 times. Last time: 14 Jul 2026 Invoice numbers: INV000001 Total volume: EUR 678.30"
        if known
        else "Confirm replacement of all records"
    )
    ui = Mock()
    ui.root.return_value = root
    ui.controls.side_effect = lambda scope, role: (
        [dialog] if scope is root else [Mock(window_text=lambda: message)]
    )
    driver = FakturamaDriver.__new__(FakturamaDriver)
    driver.ui, driver._notices = ui, []
    if known:
        driver._dismiss_information()
        ui.click.assert_called_once_with(dialog, "OK")
        assert driver._notices == [message]
    else:
        with pytest.raises(ReviewRequired, match="unrecognized"):
            driver._dismiss_information()
        ui.click.assert_not_called()


def test_clipboard_temporarily_busy_retries_without_repeating_ui_actions(monkeypatch):
    class Busy(Exception):
        pass

    backend = SimpleNamespace(
        error=Busy, OpenClipboard=Mock(side_effect=[Busy(5), Busy(5), None]), CloseClipboard=Mock()
    )
    monkeypatch.setitem(sys.modules, "win32clipboard", backend)
    monkeypatch.setattr("image_to_cash.ui.time.sleep", lambda delay: None)
    with clipboard_access():
        assert backend.OpenClipboard.call_count == 3
    backend.CloseClipboard.assert_called_once()


def test_persistently_busy_clipboard_has_a_bounded_review_outcome(monkeypatch):
    class Busy(Exception):
        pass

    backend = SimpleNamespace(
        error=Busy, OpenClipboard=Mock(side_effect=Busy(5)), CloseClipboard=Mock()
    )
    monkeypatch.setitem(sys.modules, "win32clipboard", backend)
    with pytest.raises(ReviewRequired, match="bounded"):
        with clipboard_access(timeout=0):
            raise AssertionError("Clipboard was never acquired")
    backend.CloseClipboard.assert_not_called()


def test_workspace_identity_is_a_complete_path_not_a_title_substring(tmp_path):
    target = tmp_path / "workspace/data"
    assert workspace_matches_title(f"Fakturama - {target}", target)
    assert not workspace_matches_title(f"Fakturama - {target}-other", target)


def test_strict_email_policy_stops_before_desktop_initialization(settings, source):
    assert source.customer.email.endswith(".test")
    settings.email_discrepancy_policy = "strict"
    with pytest.raises(ReviewRequired) as raised:
        WindowsAdapter(settings, "demo", source)
    assert raised.value.code == "FAKTURAMA_EMAIL_REJECTED"


def test_default_email_policy_reaches_the_desktop_driver(monkeypatch, settings, source):
    started = []

    class ImmediateExecutor:
        def __init__(self, **kwargs):
            pass

        def submit(self, function, *args):
            started.append(function)
            return SimpleNamespace(result=lambda: "driver")

    monkeypatch.setattr("image_to_cash.windows.ThreadPoolExecutor", ImmediateExecutor)
    adapter = WindowsAdapter(settings, "demo", source)
    assert adapter.driver == "driver" and started == [FakturamaDriver]


def test_customer_check_exempts_only_a_reserved_email_under_the_flag_policy(source):
    expected = source.customer
    actual = expected.model_copy(deep=True)
    actual.email = None
    verify = FakturamaDriver._verify_customer
    verify(expected, actual, "flag-reserved-domain")
    with pytest.raises(ReviewRequired, match="differ"):
        verify(expected, actual, "strict")
    actual.telephone = "+49 30 0000 0000"
    with pytest.raises(ReviewRequired) as raised:
        verify(expected, actual, "flag-reserved-domain")
    assert raised.value.evidence["fields"] == ["telephone"]
    ordinary = expected.model_copy(deep=True)
    ordinary.email = "someone@example.com"
    seen = ordinary.model_copy(deep=True)
    seen.email = None
    with pytest.raises(ReviewRequired):
        verify(ordinary, seen, "flag-reserved-domain")


def test_new_debtor_is_verified_from_a_reopened_record_not_the_saved_editor(settings, source):
    typed = source.customer.model_copy(deep=True)
    persisted = typed.model_copy(deep=True)
    persisted.email = None
    calls = []
    ui = Mock()
    ui.get.return_value = "CUST000009"
    ui.native_control.return_value.item_texts.return_value = ["Bank Transfer"]
    ui.save.side_effect = lambda: calls.append("save")
    driver = FakturamaDriver.__new__(FakturamaDriver)
    driver.ui, driver.settings = ui, settings
    driver._contact, driver._ensure_debtor_tab = Mock(), Mock()
    driver._read_customer = Mock(side_effect=lambda: calls.append("read-editor") or typed)

    def reopen(number, customer):
        calls.append("reopen")
        return persisted

    driver._reopen_contact = reopen
    result = driver.create_debtor(source.customer, "Bank Transfer")
    assert calls == ["read-editor", "save", "reopen"]
    assert result["customer"]["email"] is None and driver._customer.email is None


def test_contact_evidence_waits_for_the_tab_that_holds_the_requested_role():
    shown = {"name": None}
    tabs = [
        SimpleNamespace(window_text=lambda: "Main address"),
        SimpleNamespace(window_text=lambda: "additional address #1"),
        SimpleNamespace(window_text=lambda: "Addresses"),
    ]
    ui = Mock()
    ui.controls.return_value = tabs
    ui.tab.side_effect = lambda name, scope=None: shown.update(name=name)
    roles = {"Main address": "Invoice address", "additional address #1": "Delivery address"}
    ui.get.side_effect = lambda pane, label: roles.get(shown["name"], "")
    driver = FakturamaDriver.__new__(FakturamaDriver)
    driver.ui, driver._contact = ui, Mock(return_value="pane")
    assert driver._show_contact_role("Delivery address") == "pane"
    assert shown["name"] == "additional address #1"
    assert ui.settle.call_count == 2
    with pytest.raises(ReviewRequired) as raised:
        driver._show_contact_role("Unknown role")
    assert raised.value.code == "CONTACT_ROLE_UNAVAILABLE"


def documents_driver(settings, node_labels, rows):
    ui = Mock()
    nodes = [Mock(window_text=lambda label=label: label) for label in node_labels]
    ui.controls.return_value = nodes
    ui.navigate.return_value = "pane"
    ui.rows.return_value = rows
    ui.wait.side_effect = lambda observe, *args, **kwargs: real_wait(observe)
    driver = FakturamaDriver.__new__(FakturamaDriver)
    driver.ui, driver.settings, driver.source = ui, settings, None
    driver._customer, driver._notices = None, []
    return driver, ui, nodes


def real_wait(observe):
    value = observe()
    if not value:
        raise ReviewRequired("UI_WAIT_TIMEOUT", "condition false")
    return value


def test_new_database_without_a_document_node_reports_no_saved_documents(settings):
    driver, ui, _ = documents_driver(settings, [], [])
    assert driver.saved("Order", "WEB-2026-0714-A17") == []
    ui.search.assert_called_once_with("pane", "WEB-2026-0714-A17")


def test_missing_node_does_not_hide_a_listed_document_of_another_kind(settings):
    order_row = {
        "column0": "ICON_ORDER",
        "document": "PO000001",
        "custref": "WEB-2026-0714-A17",
        "state": "COMMAND_ORDER_PENDING",
    }
    driver, _, _ = documents_driver(settings, ["This transaction", "Orders"], [order_row])
    assert driver.saved("Invoice", "WEB-2026-0714-A17") == []


def test_existing_document_node_is_selected_before_searching(settings):
    driver, ui, nodes = documents_driver(settings, ["Orders", "Invoices"], [])
    assert driver.saved("Order", "WEB-2026-0714-A17") == []
    nodes[0].click_input.assert_called_once()
    nodes[1].click_input.assert_not_called()


def test_duplicate_document_nodes_stop_for_review(settings):
    driver, _, _ = documents_driver(settings, ["Orders", "Orders"], [])
    with pytest.raises(ReviewRequired) as raised:
        driver.saved("Order", "WEB-2026-0714-A17")
    assert raised.value.code == "CONTROL_AMBIGUOUS"


def test_only_clean_contact_editors_are_closed_before_a_fresh_read(source):
    captions = ["Fakturama", "PO000011", "Northstar Office GmbH, Marta Klein"]

    def tab(caption):
        return SimpleNamespace(window_text=lambda: caption, click_input=lambda: None)

    ui = Mock()
    ui.editor_stack.return_value.children.side_effect = lambda **kwargs: [tab(c) for c in captions]
    driver = FakturamaDriver.__new__(FakturamaDriver)
    driver.ui = ui
    driver._close_contact_editors(source.customer)
    ui.close_saved_editor.assert_called_once()
    ui.close_saved_editor.reset_mock()
    captions[2] = "*Northstar Office GmbH, Marta Klein"
    with pytest.raises(ReviewRequired) as raised:
        driver._close_contact_editors(source.customer)
    assert raised.value.code == "CONTACT_EDITOR_DIRTY"
    ui.close_saved_editor.assert_not_called()


def test_table_copy_waits_for_delayed_clipboard_publication(monkeypatch, settings):
    state = {"sentinel": None, "reads": 0}

    def read_data(format):
        state["reads"] += 1
        if state["reads"] == 1:
            return "previous clipboard"
        return state["sentinel"] if state["reads"] < 4 else "1\tSKU-01"

    def set_data(text, format):
        if text.startswith("ITC-"):
            state["sentinel"] = text

    backend = SimpleNamespace(
        error=Exception,
        CF_UNICODETEXT=13,
        OpenClipboard=Mock(),
        CloseClipboard=Mock(),
        GetClipboardData=read_data,
        IsClipboardFormatAvailable=lambda format: True,
        EmptyClipboard=Mock(),
        SetClipboardText=set_data,
    )
    monkeypatch.setitem(sys.modules, "win32clipboard", backend)
    monkeypatch.setattr("image_to_cash.ui.time.sleep", lambda delay: None)
    ui = SemanticUI.__new__(SemanticUI)
    ui.settings = settings
    table = Mock()
    ui.table = Mock(return_value=table)
    ui.focus_table = Mock()
    ui._empty_table = Mock(return_value=False)
    ui._headers = Mock(return_value=["Qty.", "SKU"])
    assert ui.copy_rows(object()) == (["Qty.", "SKU"], [["1", "SKU-01"]])
    assert sum(call.args[0] == "^a^c" for call in table.type_keys.call_args_list) == 1


@pytest.mark.parametrize("dirty", [False, True])
def test_new_customer_closes_only_an_unmodified_previously_inspected_contact(source, dirty):
    class NewContactRequested(Exception):
        pass

    ui = Mock()
    ui.get.return_value = "A different company"
    ui.editor_stack.return_value.window_text.return_value = (
        "*A different company" if dirty else "A different company"
    )
    ui.one.return_value.click_input.side_effect = NewContactRequested
    driver = FakturamaDriver.__new__(FakturamaDriver)
    driver.ui = ui
    driver._ensure_debtor_tab = Mock(return_value=object())
    if dirty:
        with pytest.raises(ReviewRequired) as raised:
            driver.begin_debtor(source.customer)
        assert raised.value.code == "UNRELATED_DEBTOR_EDITOR"
        ui.close_saved_editor.assert_not_called()
        ui.one.assert_not_called()
    else:
        with pytest.raises(NewContactRequested):
            driver.begin_debtor(source.customer)
        ui.close_saved_editor.assert_called_once()
        assert ui.one.call_args.args[1:] == ("New Contact", "Text")


def test_editor_cleanup_keeps_dirty_and_unmodified_new_drafts():
    captions = ["Fakturama", "PO000001", "*New Invoice", "New Debtor", "Customer A"]
    selected = {"caption": None}

    def tab(caption):
        return SimpleNamespace(
            window_text=lambda: caption,
            click_input=lambda: selected.update(caption=caption),
        )

    ui = SemanticUI.__new__(SemanticUI)
    ui.editor_stack = lambda: SimpleNamespace(
        children=lambda **kwargs: [tab(caption) for caption in captions]
    )
    ui.close_saved_editor = lambda: captions.remove(selected["caption"])
    result = ui.close_clean_editors()
    assert captions == ["Fakturama", "*New Invoice", "New Debtor"]
    assert result["closed_saved_editors"] == ["PO000001", "Customer A"]


@pytest.mark.parametrize("shown,multiplier,accepted", [("-5%", -1, True), ("5%", -1, False)])
def test_document_discount_readback_checks_fakturama_display_sign(shown, multiplier, accepted):
    ui = SemanticUI.__new__(SemanticUI)
    control = Mock()
    ui.field, ui.native_control = Mock(return_value=control), Mock(return_value=Mock())
    ui.activate, ui.wait = Mock(), Mock()
    ui.value = lambda _: shown
    if accepted:
        ui.set_number(object(), "Discount", Decimal("5"), display_multiplier=multiplier)
    else:
        with pytest.raises(ReviewRequired, match="did not retain"):
            ui.set_number(object(), "Discount", Decimal("5"), display_multiplier=multiplier)


@pytest.mark.parametrize("identical", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_selector_enumerates_all_rows_and_preserves_identical_duplicate_records(identical, changed):
    first, last = ["DUPLICATE"], ["DUPLICATE" if identical else "SECOND"]
    headers = ["No."]
    ui = SemanticUI.__new__(SemanticUI)
    table = Mock()
    ui.table, ui.focus_table = Mock(return_value=table), Mock()
    ui.selected_row_number = Mock(side_effect=[2, 1, 1, 2, 2])
    ui.copy_rows = Mock(
        side_effect=[
            (headers, [first]),
            (headers, [last]),
            (headers, [first]),
            (headers, [["CHANGED"] if changed else last]),
        ]
    )
    if changed:
        with pytest.raises(ReviewRequired, match="changed during enumeration"):
            ui.copy_list_rows(object())
    else:
        assert ui.copy_list_rows(object()) == (headers, [first, last])


def selected_row_ui(monkeypatch, settings, clipboard_row):
    state = {"sentinel": None, "reads": 0}

    def read_data(format):
        state["reads"] += 1
        return "previous" if state["reads"] == 1 else clipboard_row

    backend = SimpleNamespace(
        error=Exception,
        CF_UNICODETEXT=13,
        OpenClipboard=Mock(),
        CloseClipboard=Mock(),
        GetClipboardData=read_data,
        IsClipboardFormatAvailable=lambda format: True,
        EmptyClipboard=Mock(),
        SetClipboardText=Mock(),
    )
    monkeypatch.setitem(sys.modules, "win32clipboard", backend)
    ui = SemanticUI.__new__(SemanticUI)
    ui.settings = settings
    ui.table = Mock(return_value=Mock())
    ui.focus_table, ui._empty_table = Mock(), Mock(return_value=False)
    # After a selection change the header pixels merge into one cell; they must not be reread.
    ui._headers = Mock(return_value=["No. FirstName Name Company ZIP City"])
    return ui


def test_selected_row_export_reuses_established_headers(monkeypatch, settings):
    ui = selected_row_ui(monkeypatch, settings, "CUST000001\tMarta\tKlein")
    headers = ["No.", "First Name", "Name"]
    result = ui.copy_rows(object(), current_selection=True, known_headers=headers)
    assert result == (headers, [["CUST000001", "Marta", "Klein"]])
    ui._headers.assert_not_called()


def test_selected_row_export_with_a_different_shape_still_stops(monkeypatch, settings):
    ui = selected_row_ui(monkeypatch, settings, "CUST000001\tMarta")
    with pytest.raises(ReviewRequired) as raised:
        ui.copy_rows(object(), current_selection=True, known_headers=["No.", "First Name", "Name"])
    assert raised.value.code == "TABLE_EXPORT_PROFILE_UNKNOWN"


def items_table(
    selected_rows,
    total_rows=17,
    widths=(49, 40, 71, 59, 160, 90, 129, 69, 73, 69),
    scrollbar=False,
):
    """Draw an Items table as observed: fill 160, separators 192, painted or plain rows."""
    from PIL import Image, ImageDraw

    row, width = 25, 1452
    image = Image.new("RGB", (width, row * (total_rows + 1)), "white")
    draw = ImageDraw.Draw(image)
    edges, x = [], 0
    for size in widths:
        x += size
        edges.append(x)
    draw.rectangle((0, 0, edges[0] - 1, row - 1), fill=(240, 240, 240))
    draw.rectangle((edges[0], 0, edges[-1], row - 1), fill=(160, 160, 160))
    for index in range(1, total_rows + 1):
        top = index * row
        if index <= selected_rows:
            draw.rectangle((edges[0], top, edges[-1], top + row - 2), fill=(0, 120, 215))
        draw.line((0, top + row - 1, width, top + row - 1), fill=(190, 190, 190))
        for edge in edges:
            colour = (255, 255, 255) if index <= selected_rows else (190, 190, 190)
            draw.line((edge, top, edge, top + row - 1), fill=colour)
    for edge in edges:
        draw.line((edge, 0, edge, row - 1), fill=(192, 192, 192))
    # Anti-aliased header text leaves a few stray light columns inside the first cell.
    for x, shade in ((21, 189), (28, 183), (30, 189)):
        draw.line((x, 0, x, row - 1), fill=(shade, shade, shade))
    # The header's bottom border spans the full width, past the last column.
    draw.line((0, row - 1, width, row - 1), fill=(192, 192, 192))
    if scrollbar:
        # A document longer than the viewport adds a scrollbar at the right edge.
        draw.rectangle((width - 20, 0, width - 1, image.height), fill=(240, 240, 240))
        draw.line((width - 12, 0, width - 12, image.height), fill=(133, 133, 133))
    return image, edges


@pytest.mark.parametrize("scrollbar", [False, True])
@pytest.mark.parametrize("selected", [1, 2, 8, 12, 15, 17])
def test_column_edges_survive_any_selection_with_or_without_a_scrollbar(selected, scrollbar):
    import numpy as np

    image, edges = items_table(selected, scrollbar=scrollbar)
    gray = np.array(image.convert("L"))
    observed, height = SemanticUI.column_edges(image, gray, 9)
    assert height in {24, 25}
    assert observed == [0, *edges]


def test_column_edges_stop_when_columns_cannot_be_accounted_for():
    import numpy as np

    image, _ = items_table(2)
    with pytest.raises(ReviewRequired) as raised:
        SemanticUI.column_edges(image, np.array(image.convert("L")), 3)
    assert raised.value.code == "TABLE_COLUMNS_CLIPPED"


def test_document_net_uses_post_discount_total_and_preserves_subtotal_meaning():
    totals = document_totals(Decimal("100"), Decimal("18.05"), Decimal("113.05"), Decimal("5"))
    assert totals.net == Decimal("95") and totals.gross == Decimal("113.05")
    assert document_totals(
        Decimal("100"), Decimal("19"), Decimal("119"), Decimal("0")
    ).net == Decimal("100")
