"""Windows UI primitives. All targets come from the current scoped UI, never saved coordinates."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import time
import uuid
import zipfile
from builtins import set as Set
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ocr import OCRDocument

from .config import Settings
from .models import ReviewRequired


def key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold()) or text.strip().casefold()


@contextmanager
def clipboard_access(timeout=3):
    import win32clipboard

    deadline = time.monotonic() + timeout
    while True:
        try:
            win32clipboard.OpenClipboard()
            break
        except win32clipboard.error as exc:
            code = getattr(exc, "winerror", exc.args[0])
            if code != 5:
                raise ReviewRequired(
                    "CLIPBOARD_FAILURE",
                    "Windows clipboard access failed",
                    evidence={"native_code": code},
                ) from None
            if time.monotonic() >= deadline:
                raise ReviewRequired(
                    "CLIPBOARD_BUSY",
                    "Windows clipboard remained busy after bounded acquisition retries",
                ) from None
            time.sleep(0.025)
    try:
        yield
    finally:
        win32clipboard.CloseClipboard()


class SemanticUI:
    def __init__(self, settings: Settings, workspace: str):
        import os

        if os.name != "nt":
            raise ReviewRequired("WINDOWS_REQUIRED", "Live Fakturama automation requires Windows")
        from pywinauto import Application, Desktop

        from .doctor import desktop_unlocked, fakturama_path, workspace_matches_title

        if not desktop_unlocked():
            raise ReviewRequired(
                "DESKTOP_UNAVAILABLE", "An unlocked interactive desktop is required"
            )
        self.settings, self.workspace = settings, workspace
        self.desktop = Desktop(backend="uia")
        roots = [w for w in self.desktop.windows() if w.window_text().startswith("Fakturama -")]
        if len(roots) != 1:
            raise ReviewRequired(
                "FAKTURAMA_NOT_READY", "Open exactly one initialized Fakturama test workspace"
            )
        root = roots[0]
        expected = settings.workspace_path(workspace) / "data"
        if not workspace_matches_title(root.window_text(), expected):
            raise ReviewRequired(
                "WORKSPACE_MISMATCH",
                f"Fakturama must show the dedicated workspace {settings.workspace_path(workspace) / 'data'}",
            )
        self.handle, self.pid = root.handle, root.process_id()
        self.native = Application(backend="win32").connect(process=self.pid)
        executable = fakturama_path(settings)
        self.app_root = executable.parent if executable else None
        self._header_cells: dict[int, dict[str, tuple[int, int]]] = {}
        self._header_text_cache: dict[str, OCRDocument] = {}
        self._row_number_cache: dict[str, int] = {}
        self._row_header_width: dict[int, int] = {}

    def root(self):
        return self.desktop.window(handle=self.handle).wrapper_object()

    def activate(self):
        import win32gui
        import win32process

        if win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())[1] != self.pid:
            self.root().set_focus()
        self.wait(
            lambda: win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())[1],
            lambda pid: pid == self.pid,
        )

    @staticmethod
    def visible(control) -> bool:
        rect = control.rectangle()
        return control.is_visible() and rect.width() > 1 and rect.height() > 1

    def controls(self, scope, role=None):
        return [
            c
            for c in scope.descendants(**({"control_type": role} if role else {}))
            if self.visible(c)
        ]

    def one(self, scope, name: str, role: str):
        exact = [c for c in scope.descendants(title=name, control_type=role) if self.visible(c)]
        if len(exact) == 1:
            return exact[0]
        matches = [c for c in self.controls(scope, role) if key(c.window_text()) == key(name)]
        if len(matches) != 1:
            raise ReviewRequired(
                "CONTROL_AMBIGUOUS",
                f"Expected one visible {role} labelled {name!r}; observed {len(matches)}",
            )
        return matches[0]

    def wait(self, observe, predicate=lambda value: bool(value), *, timeout=None):
        deadline = time.monotonic() + (timeout or self.settings.desktop_timeout_seconds)
        last_error = None
        while time.monotonic() < deadline:
            try:
                value = observe()
                if predicate(value):
                    return value
            except Exception as exc:
                if isinstance(exc, (AttributeError, TypeError, NameError)):
                    raise
                last_error = exc
            time.sleep(0.1)
        raise ReviewRequired(
            "UI_WAIT_TIMEOUT",
            f"Expected UI condition was not observed ({type(last_error).__name__ if last_error else 'condition false'})",
            evidence={"last_code": last_error.code, "last_message": str(last_error)}
            if isinstance(last_error, ReviewRequired)
            else {},
        )

    def pane(self, name: str):
        matches = [
            p for p in self.controls(self.root(), "Pane") if key(p.window_text()) == key(name)
        ]
        if len(matches) != 1:
            raise ReviewRequired(
                "PANE_UNAVAILABLE", f"Expected one active pane {name!r}; observed {len(matches)}"
            )
        return matches[0]

    def tab(self, name: str, *, scope=None):
        control = self.one(scope or self.root(), name, "TabItem")
        control.click_input()
        self.wait(lambda: self.one(scope or self.root(), name, "TabItem").is_selected())

    def click(self, scope, name: str, role="Button"):
        control = self.one(scope, name, role)
        if not control.is_enabled():
            raise ReviewRequired("CONTROL_DISABLED", f"{name!r} is disabled")
        try:
            control.invoke()
        except Exception:
            control.click_input()

    def field(self, scope, label: str, *, role="Edit", index=None):
        if index is None:
            exact = [
                c for c in scope.descendants(title=label, control_type=role) if self.visible(c)
            ]
            if len(exact) == 1:
                return exact[0]
        named = [c for c in self.controls(scope, role) if key(c.window_text()) == key(label)]
        if len(named) == 1 and index is None:
            return named[0]
        anchors = [
            c
            for role in ("Text", "CheckBox")
            for c in self.controls(scope, role)
            if key(c.window_text()) == key(label)
        ]
        if len(anchors) != 1:
            raise ReviewRequired(
                "FIELD_ANCHOR_AMBIGUOUS", f"Expected one text or checkbox anchor for {label!r}"
            )
        anchor = anchors[0].rectangle()
        candidates = []
        for control in self.controls(scope, role):
            rect = control.rectangle()
            overlap = min(anchor.bottom, rect.bottom) - max(anchor.top, rect.top)
            if rect.left >= anchor.right and overlap > min(anchor.height(), rect.height()) * 0.35:
                candidates.append(control)
        candidates.sort(key=lambda c: c.rectangle().left)
        if index is not None:
            if len(candidates) <= index:
                raise ReviewRequired(
                    "FIELD_UNAVAILABLE", f"Grouped field {label!r} has insufficient inputs"
                )
            return candidates[index]
        if not candidates:
            raise ReviewRequired("FIELD_UNAVAILABLE", f"No input associated with {label!r}")
        return candidates[0]

    def value(self, control) -> str:
        # SWT's search Edit exposes an empty UIA Value even while WM_GETTEXT
        # returns its visible text. Use the standard native Edit implementation.
        if control.element_info.control_type == "Edit" and control.handle:
            return self.native_control(control).window_text()
        if (
            control.element_info.control_type == "ComboBox"
            and control.element_info.class_name == "ComboBox"
        ):
            return self.native_control(control).selected_text()
        try:
            return str(control.iface_value.CurrentValue)
        except Exception:
            try:
                return control.selected_text()
            except Exception:
                return control.window_text()

    def get(self, scope, label, *, role="Edit", index=None):
        return self.value(self.field(scope, label, role=role, index=index))

    def set(self, scope, label, value, *, index=None):
        control = self.field(scope, label, index=index)
        native = self.native_control(control)
        native.set_edit_text(str(value))
        # Multi-line SWT fields insert a literal tab. Move focus to another
        # observed edit instead of synthesizing Tab inside those controls.
        others = [
            c for c in self.controls(scope, "Edit") if c.handle != control.handle and c.is_enabled()
        ]
        if others:
            others[0].set_focus()
        actual = self.value(control)
        if actual.strip() != str(value).strip():
            raise ReviewRequired(
                "FIELD_WRITE_UNCONFIRMED",
                f"Field {label!r} did not retain the requested text",
                evidence={"expected": str(value), "observed": actual},
            )

    def combo(self, scope, label, value):
        control = self.field(scope, label, role="ComboBox")
        self.select_combo(control, value)

    def select_combo(self, control, value):
        if key(self.value(control)) == key(str(value)):
            return
        # The SWT Win32 ComboBox UIA selection changes the visible item without
        # reliably notifying Fakturama. Win32 select sends CBN_SELCHANGE as well.
        if control.element_info.class_name == "ComboBox":
            native = self.native_control(control)
            indices = [
                i for i, text in enumerate(native.item_texts()) if key(text) == key(str(value))
            ]
            if len(indices) != 1:
                raise ReviewRequired(
                    "COMBO_CHOICE_AMBIGUOUS", f"Expected one exact dropdown choice {value!r}"
                )
            native.select(indices[0])
            self.wait(lambda: self.value(control), lambda actual: key(actual) == key(str(value)))
            return
        try:
            control.expand()
            choices = [
                c
                for c in control.descendants(control_type="ListItem")
                if key(c.window_text()) == key(str(value))
            ]
            if len(choices) != 1:
                control.collapse()
                raise ReviewRequired(
                    "COMBO_CHOICE_AMBIGUOUS", f"Expected one exact dropdown choice {value!r}"
                )
            try:
                choices[0].select()
            except Exception:
                choices[0].click_input()
            try:
                control.collapse()
            except Exception:
                pass
        except ReviewRequired:
            raise
        except Exception:
            control.select(str(value))
        if key(self.value(control)) != key(str(value)):
            raise ReviewRequired(
                "COMBO_SELECTION_UNCONFIRMED", f"Dropdown did not select {value!r}"
            )

    @staticmethod
    def decimal(text):
        from decimal import Decimal

        match = re.search(r"-?\d[\d.,]*", str(text))
        if not match:
            raise ReviewRequired(
                "NUMBER_UNREADABLE", "Application field has no readable numeric value"
            )
        numeric = match.group()
        # This adapter pins en_DE display formatting; clipboard exports without a
        # comma use canonical decimal points. Source-image parsing stays stricter.
        return Decimal(numeric.replace(".", "").replace(",", ".") if "," in numeric else numeric)

    def set_number(self, scope, label, value, *, display_multiplier=1):
        control = self.field(scope, label)
        self.activate()
        native = self.native_control(control)
        control.set_focus()
        try:
            self.wait(control.has_keyboard_focus, timeout=2)
        except ReviewRequired:
            control = self.field(scope, label)
            control.click_input()
            self.wait(control.has_keyboard_focus, timeout=2)
            native = self.native_control(control)
        native.type_keys("^a" + str(value).replace(".", ",") + "{TAB}", set_foreground=False)
        if self.decimal(self.value(control)) * display_multiplier != value:
            raise ReviewRequired(
                "NUMBER_WRITE_UNCONFIRMED",
                f"Numeric field {label!r} did not retain its requested value",
            )

    def set_date(self, scope, label, desired):
        # Nebula CDateTime handles date segments, rather than a free text value.
        # The tested en_DE profile is day/month/year and advances each full segment.
        control = self.field(scope, label)
        native = self.native_control(control)
        self.activate()
        import win32con
        import win32gui

        day = re.match(r"\d{1,2}(?=\s|[./-])", self.value(control))
        if day is None:
            raise ReviewRequired(
                "DATE_PROFILE_UNSUPPORTED",
                "The application date does not use the observed day-first profile",
            )
        start = win32gui.SendMessage(native.handle, win32con.EM_POSFROMCHAR, 0, 0)
        end = win32gui.SendMessage(native.handle, win32con.EM_POSFROMCHAR, day.end(), 0)
        width, height = win32gui.GetClientRect(native.handle)[2:]
        left, right = start & 65535, end & 65535
        if start < 0 or end < 0 or not 0 <= left < right < width:
            raise ReviewRequired(
                "DATE_SEGMENT_UNAVAILABLE",
                "The native date input did not expose its current day glyph positions",
            )
        # UIA focus alone can leave Nebula without a selected segment. Target the
        # day text using positions reported by this current native Edit control.
        control.click_input(coords=((left + right) // 2, height // 2))
        self.wait(lambda: native.selection_indices(), lambda pair: pair[0] == 0 and pair[1] > 0)
        native.type_keys(desired.strftime("%d%m%Y"), set_foreground=False)
        self.one(scope, label, "Text").click_input()
        from .windows import parse_day

        self.wait(lambda: self.value(control), lambda value: parse_day(value) == desired)

    def editor_stack(self):
        matches = [
            t
            for t in self.controls(self.root(), "Tab")
            if any(c.window_text() == "Fakturama" for c in t.children(control_type="TabItem"))
        ]
        if len(matches) != 1:
            raise ReviewRequired(
                "EDITOR_STACK_UNAVAILABLE", "Cannot establish the active editor stack"
            )
        return matches[0]

    def maximize_editor(self):
        self.activate()
        self.native_control(self.root()).maximize()
        stack = self.editor_stack()
        if any(b.window_text() == "Maximize" for b in stack.descendants(control_type="Button")):
            self.click(stack, "Maximize")

    def restore_parts(self):
        stack = self.editor_stack()
        if any(b.window_text() == "Restore" for b in stack.descendants(control_type="Button")):
            self.click(stack, "Restore")

    def close_clean_editors(self):
        """Release saved editor instances; preserve every dirty or new draft."""
        captions = [
            tab.window_text() for tab in self.editor_stack().children(control_type="TabItem")
        ]
        closed, preserved = [], []
        for caption in captions:
            if key(caption) == "fakturama":
                continue
            if caption.lstrip().startswith("*") or key(caption).startswith("new"):
                preserved.append(caption)
                continue
            matches = [
                tab
                for tab in self.editor_stack().children(control_type="TabItem")
                if tab.window_text() == caption
            ]
            if len(matches) != 1:
                raise ReviewRequired(
                    "EDITOR_CLEANUP_AMBIGUOUS", "Saved editor names are not unique"
                )
            matches[0].click_input()
            self.close_saved_editor()
            closed.append(caption)
        return {"closed_saved_editors": closed, "preserved_drafts": preserved}

    def roles(self, scope, wanted: Set[str]):
        field = self.field(scope, "address type")
        buttons = field.parent().children(control_type="Button")
        if len(buttons) != 1:
            raise ReviewRequired(
                "ADDRESS_ROLE_CONTROL_AMBIGUOUS", "Cannot establish the address role dropdown"
            )
        buttons[0].click_input()
        for label in ("Invoice address", "Delivery address"):
            check = self.one(self.root(), label, "CheckBox")
            if bool(check.get_toggle_state()) != (label in wanted):
                check.click_input()
            self.wait(
                lambda check=check: bool(check.get_toggle_state()),
                lambda actual, label=label: actual == (label in wanted),
            )
        self.one(scope, "address type", "Text").click_input()

    def cell(self, scope, row: int, column: str, value):
        headers, rows = self.copy_rows(scope)
        indices = [i for i, header in enumerate(headers) if key(header) == key(column)]
        if len(indices) != 1 or row >= len(rows):
            raise ReviewRequired(
                "CELL_UNAVAILABLE", f"Cannot establish row {row + 1}, column {column!r}"
            )
        index = indices[0]
        existing = rows[row][index]
        if isinstance(value, str):
            equal = existing.strip() == value.strip()
        else:
            equal = self.decimal(existing) * (-100 if key(column) == "discount" else 1) == value
        if equal:
            return
        table = self.select_row(scope, row)
        table.scroll("left", "end")
        self._headers(table, len(headers), scan=False)
        point = self._header_cells.get(table.handle, {}).get(key(column))
        if point is None:
            table.scroll("right", "end")
            self._headers(table, len(headers), scan=False)
            point = self._header_cells.get(table.handle, {}).get(key(column))
        if point is None:
            raise ReviewRequired(
                "CELL_GEOMETRY_UNAVAILABLE", f"The current header does not locate {column!r}"
            )
        target_x, _ = point
        target_y = self.row_point(table)[1]
        table.click_input(coords=(target_x, target_y))

        if key(column) == "description":
            dialog = self.dialog("position description")
            edit = self.field(dialog, "Please enter a descriptive text:")
            self.native_control(edit).set_edit_text(str(value).replace("\n", "\r\n"))
            self.click(dialog, "OK")
            self.wait(lambda: self.controls(self.root(), "Window"), lambda windows: not windows)
            _, after = self.copy_rows(scope)
            if after[row][index].strip() != str(value).strip():
                raise ReviewRequired(
                    "CELL_WRITE_UNCONFIRMED", "Saved position description differs from the source"
                )
            return

        def editor():
            rect = table.rectangle()
            choices = [
                e
                for e in self.controls(scope, "Edit")
                if rect.left <= e.rectangle().left < rect.right
                and rect.top <= e.rectangle().top < rect.bottom
            ]
            return choices[0] if len(choices) == 1 else None

        edit = self.wait(editor)
        rect = edit.rectangle()
        table_rect = table.rectangle()
        if not (
            rect.left <= table_rect.left + target_x < rect.right
            and rect.top <= table_rect.top + target_y < rect.bottom
        ):
            raise ReviewRequired(
                "CELL_EDITOR_MISMATCH", "The inline editor is not inside the observed target cell"
            )
        if isinstance(value, str):
            self.native_control(edit).set_edit_text(value)
        else:
            self.native_control(edit).type_keys(
                "^a" + str(value).replace(".", ","), set_foreground=False
            )
        edit.type_keys("{ENTER}", set_foreground=False)
        self.field(scope, "Cust.Ref.").click_input()
        self.wait(editor, lambda result: result is None)
        _, after = self.copy_rows(scope)
        actual = after[row][index]
        numeric_actual = (
            self.decimal(actual) * (-100 if key(column) == "discount" else 1)
            if not isinstance(value, str)
            else None
        )
        equal = (
            actual.strip() == value.strip() if isinstance(value, str) else numeric_actual == value
        )
        if not equal:
            raise ReviewRequired(
                "CELL_WRITE_UNCONFIRMED",
                f"Table cell {column!r} did not retain the source value",
                evidence={"expected": str(value), "observed": actual},
            )

    def navigate(self, name):
        nav = self.pane("Navigation View")
        self.one(nav, name, "Text").click_input()
        return self.wait(lambda: self.pane(name))

    def dialog(self, name):
        return self.wait(lambda: self.one(self.root(), name, "Window"))

    def save(self):
        self.activate_editor()
        self.wait(lambda: self.one(self.root(), "Save the current contents", "Button").is_enabled())
        self.click(self.root(), "Save the current contents")
        self.wait(
            lambda: self.editor_stack().window_text(), lambda caption: not caption.startswith("*")
        )

    def close_saved_editor(self):
        self.activate_editor()
        caption = self.editor_stack().window_text()
        before = len(self.editor_stack().children(control_type="TabItem"))
        if caption.startswith("*"):
            raise ReviewRequired("DIRTY_EDITOR", "Cannot close an editor with unresolved changes")
        self.root().type_keys("^w", set_foreground=False)
        self.wait(
            lambda: len(self.editor_stack().children(control_type="TabItem")),
            lambda count: count < before,
        )

    def activate_editor(self):
        self.activate()
        stack = self.editor_stack()
        caption = stack.window_text()
        tabs = [
            tab
            for tab in stack.children(control_type="TabItem")
            if tab.window_text() == caption and tab.is_selected()
        ]
        if len(tabs) != 1:
            raise ReviewRequired(
                "ACTIVE_EDITOR_AMBIGUOUS", "Cannot identify the current editor tab"
            )
        tabs[0].click_input()

    def native_control(self, scope):
        return self.native.window(handle=scope.handle).wrapper_object()

    def table(self, scope):
        self.activate()
        native = self.native_control(scope)
        choices = []
        for control in native.descendants():
            rect = control.rectangle()
            if (
                control.class_name().startswith("SWT")
                and control.is_visible()
                and rect.width() > scope.rectangle().width() * 0.5
                and rect.height() > 60
                and all(
                    child.class_name() in {"Edit", "ComboBox"}
                    and child.rectangle().height() < rect.height() * 0.5
                    for child in control.children()
                    if child.is_visible()
                )
            ):
                choices.append(control)
        if len(choices) != 1:
            raise ReviewRequired(
                "TABLE_UNAVAILABLE", f"Expected one visible NatTable canvas; found {len(choices)}"
            )
        return choices[0]

    def focus_table(self, table):
        self.activate()
        uia = self.desktop.window(handle=table.handle).wrapper_object()
        try:
            uia.set_focus()
            self.wait(uia.has_keyboard_focus, timeout=2)
        except Exception:
            table.click_input(coords=self.row_point(table, first=True))
            self.wait(uia.has_keyboard_focus, timeout=2)

    def search(self, scope, text):
        control = self.field(scope, "Search:")
        control.set_edit_text(text)
        control.set_focus()
        if self.value(control) != text:
            raise ReviewRequired("SEARCH_UNCONFIRMED", "Search query did not persist")
        self.settle(self.table(scope), seconds=0.8)

    def settle(self, control, *, seconds=0.5):
        """Wait until a control's pixels stop changing; selection can precede its repaint."""
        previous, stable_since = None, time.monotonic()

        def settled():
            nonlocal previous, stable_since
            current = control.capture_as_image().tobytes()
            if current != previous:
                previous, stable_since = current, time.monotonic()
            return time.monotonic() - stable_since >= seconds

        self.wait(settled)

    def copy_rows(
        self, scope, *, current_selection=False, known_headers: list[str] | None = None
    ) -> tuple[list[str], list[list[str]]]:
        import win32clipboard

        table = self.table(scope)
        self.focus_table(table)
        if not current_selection:
            table.type_keys("^{HOME}", set_foreground=False)
        # Fakturama 2.2.0's NatTable copy handler throws when the filtered body is
        # empty. Establish an empty body visually before sending any copy command.
        if self._empty_table(table):
            return [], []
        sentinel = f"ITC-{uuid.uuid4()}"
        with clipboard_access():
            old = (
                win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT)
                else None
            )
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(sentinel, win32clipboard.CF_UNICODETEXT)
        try:

            def copied_text():
                with clipboard_access():
                    return (
                        win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT)
                        else ""
                    )

            for attempt in range(2):
                self.focus_table(table)
                table.type_keys("^c" if current_selection else "^a^c", set_foreground=False)
                try:
                    text = self.wait(
                        copied_text, lambda value: value not in {sentinel, ""}, timeout=3
                    )
                    break
                except ReviewRequired:
                    if self._empty_table(table):
                        return [], []
                    if attempt:
                        raise ReviewRequired(
                            "TABLE_COPY_UNCONFIRMED",
                            "The current table did not publish readable clipboard rows after bounded read-only copy retries",
                        ) from None
                    table = self.table(scope)
            rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
            if not rows or len({len(row) for row in rows}) != 1:
                raise ReviewRequired(
                    "TABLE_SHAPE_AMBIGUOUS", "Clipboard rows did not form a stable table"
                )
            if known_headers is not None:
                # A selected-row state redraws the header rulings, so its pixels cannot
                # re-establish columns. Reuse this table's headers and require the same shape.
                if any(len(row) != len(known_headers) for row in rows):
                    raise ReviewRequired(
                        "TABLE_EXPORT_PROFILE_UNKNOWN",
                        "Selected-row export differs from the established table columns",
                    )
                return list(known_headers), rows
            headers = self._headers(table, len(rows[0]))
            if [key(h) for h in headers] == [
                "no",
                "firstname",
                "name",
                "company",
                "zip",
                "city",
            ] and len(rows[0]) == 7:
                # Fakturama 2.2.0 exports its hidden contact-name-addon column
                # between Name and Company. Require the complete visible signature;
                # master editors independently verify each selected record later.
                rows = [row[:3] + row[4:] for row in rows]
            if any(len(row) != len(headers) for row in rows):
                raise ReviewRequired(
                    "TABLE_EXPORT_PROFILE_UNKNOWN",
                    "Clipboard column count differs from the observed visible table",
                )
            return headers, rows
        finally:
            with clipboard_access():
                win32clipboard.EmptyClipboard()
                if old is not None:
                    win32clipboard.SetClipboardText(old, win32clipboard.CF_UNICODETEXT)

    def _headers(self, table, expected: int, *, scan=True) -> list[str]:
        import cv2
        import numpy as np

        from .ocr import recognize

        image = table.capture_as_image()
        gray = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        edges, header_height = self.column_edges(image, gray, expected)
        from PIL import Image

        normalized = np.full((header_height, edges[-1]), 255, dtype=np.uint8)
        for left, right in zip(edges, edges[1:], strict=False):
            cell = gray[:header_height, left:right]
            thresholded = cv2.threshold(cell, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
            # Selected light-blue headers can have white text even when their
            # grey median exceeds 200. Normalize the thresholded background,
            # rather than guessing text polarity from the original brightness.
            if float(np.median(thresholded)) < 128:
                thresholded = cv2.bitwise_not(thresholded)
            normalized[:, left:right] = thresholded
        fingerprint = hashlib.sha256(
            str(normalized.shape).encode() + normalized.tobytes()
        ).hexdigest()
        if fingerprint not in self._header_text_cache:
            path = self.settings.runtime / "cache/ui-table-headers.png"
            Image.fromarray(normalized).convert("RGB").save(path)
            document = recognize(path, self.settings, engine="tesseract")
            if len(self._header_text_cache) >= 64:
                self._header_text_cache.pop(next(iter(self._header_text_cache)))
            self._header_text_cache[fingerprint] = document
        document = self._header_text_cache[fingerprint]
        headers = [
            " ".join(
                w.text
                for w in sorted(document.words, key=lambda w: w.box.x)
                if left <= w.box.x + w.box.width / 2 < right
            )
            for left, right in zip(edges, edges[1:], strict=False)
        ]
        observed_cells = {
            key(header): ((left + right) // 2, header_height)
            for header, left, right in zip(headers, edges, edges[1:], strict=False)
            if header.strip()
        }
        if headers and not re.search(r"[A-Za-z0-9]", headers[0]):
            self._row_header_width[table.handle] = edges[1]
        else:
            self._row_header_width.pop(table.handle, None)
        document_table = bool(headers and key(headers[0]) in {"pos", "position"})
        if document_table or (
            len(headers) == expected + 1 and not re.search(r"[A-Za-z0-9]", headers[0])
        ):
            headers = headers[1:]  # Selector row-number header is not copied.
        if document_table and scan:
            profile = [
                "Qty.",
                "Item No.",
                "Picture",
                "Name",
                "Description",
                "VAT",
                "U.Price",
                "Discount",
                "Price",
            ]
            seen = {key(header) for header in headers}
            if not {key(header) for header in profile}.issubset(seen):
                table.scroll("right", "end")
                try:
                    seen.update(
                        key(header) for header in self._headers(table, expected, scan=False)
                    )
                finally:
                    table.scroll("left", "end")
            if expected != len(profile) or not {key(header) for header in profile}.issubset(seen):
                raise ReviewRequired(
                    "TABLE_COLUMNS_CLIPPED",
                    "Could not observe every item column across the current horizontal viewport",
                )
            headers = profile
        self._header_cells[table.handle] = observed_cells
        return headers

    @staticmethod
    def column_edges(image, gray, expected: int) -> tuple[list[int], int]:
        """Find column boundaries from current pixels; raises rather than guessing."""
        import cv2
        import numpy as np

        binary = cv2.threshold(gray, 235, 255, cv2.THRESH_BINARY_INV)[1]
        # Painted rows and the solid header read as ink, and once they fill half the table
        # every pixel column looks like a ruling. Judge rulings only against the plain rows;
        # with too few of them, the header's own separators below decide the columns.
        header_height = SemanticUI.header_height(gray)
        ignored = SemanticUI.painted_rows(image)
        ignored[:header_height] = True
        plain = image.height - int(ignored.sum())
        xs: list[int] = []
        if plain >= 40:
            rulings = binary.copy()
            rulings[ignored] = 0
            vertical = cv2.morphologyEx(
                rulings, cv2.MORPH_OPEN, np.ones((max(20, plain // 3), 1), np.uint8)
            )
            xs = [i for i, count in enumerate((vertical > 0).sum(axis=0)) if count > plain * 0.5]
        edges = [0]
        for x in xs:
            if x - edges[-1] > 4:
                # A blank stretch of header after real columns ends the table. A scrollbar
                # can follow it. Only the first cell may be blank, such as a row-number gutter.
                band = gray[1 : header_height - 1, edges[-1] + 2 : x - 1]
                if len(edges) > 1 and (band > 235).all():
                    break
                edges.append(x)
        if image.width - edges[-1] > 4 and len(edges) <= expected + 1:
            tail = binary[:20, edges[-1] + 2 :]
            if (tail > 0).sum() > 30:
                edges.append(image.width)
        horizontal = cv2.morphologyEx(
            binary, cv2.MORPH_OPEN, np.ones((1, max(20, image.width // 8)), np.uint8)
        )
        rules = [
            i
            for i, count in enumerate((horizontal > 0).sum(axis=1))
            if count > image.width * 0.2 and i > 5
        ]
        if not rules:
            raise ReviewRequired(
                "TABLE_HEADER_UNAVAILABLE", "Cannot establish the current table header boundary"
            )
        if len(edges) <= 2:
            # Dialog NatTables draw vertical separators in their headers only.
            # Selected headers have bright rules; the empty trailing area is a
            # wide run and marks the end of the last real column.
            bright = [
                i
                for i, count in enumerate((gray[:header_height] > 235).sum(axis=0))
                if count > header_height * 0.85
            ]
            runs: list[list[int]] = []
            for x in bright:
                if not runs or x > runs[-1][-1] + 1:
                    runs.append([x])
                else:
                    runs[-1].append(x)
            edges = [0]
            for run in runs:
                if len(run) < 5:
                    edges.append(run[len(run) // 2])
                elif run[0] > 5:
                    edges.append(run[0])
            if len(edges) - 1 not in {expected, expected + 1}:
                edges = SemanticUI.separator_edges(gray, header_height)
        if not 1 <= len(edges) - 1 <= expected + 1:
            raise ReviewRequired(
                "TABLE_COLUMNS_CLIPPED",
                "Visible table headers do not cover the copied columns; resize the pane or dialog",
            )
        return edges, header_height

    @staticmethod
    def separator_edges(gray, header_height: int) -> list[int]:
        """Columns of a fully selected table: thin separators lighter than the header fill."""
        import numpy as np

        # The header's bottom border spans the full width, so leave that row out.
        lightest = gray[1 : header_height - 1].min(axis=0).astype(int)
        dark = lightest[lightest < 235]
        if not dark.size:
            return [0]
        fill = int(np.bincount(dark).argmax())
        lighter = lightest[(lightest > fill + 15) & (lightest < 235)]
        if not lighter.size:
            return [0]
        # Separators share one value; anti-aliased header text produces only stray ones.
        separator = int(np.bincount(lighter).argmax())
        edges = [0]
        for x, value in enumerate(lightest):
            if value == separator and x - edges[-1] > 4:
                edges.append(x)
        # The last separator ends the last column. What follows is empty space or, on a
        # document longer than the viewport, a scrollbar; neither is a column.
        return edges

    @staticmethod
    def painted_rows(image):
        """Boolean per pixel row: mostly selection colour, so it holds no usable ruling."""
        import numpy as np

        colors = np.array(image.convert("RGB")).astype(float)
        blue = (colors[:, :, 2] - colors[:, :, 0] > 12) & (colors[:, :, 1] - colors[:, :, 0] > 3)
        return blue.sum(axis=1) > image.width * 0.25

    @staticmethod
    def header_height(gray) -> int:
        import cv2
        import numpy as np

        medians = np.median(gray[:, : max(1, gray.shape[1] // 3)], axis=1)
        base = float(np.median(medians[2:6]))
        for y in range(6, min(100, len(medians) - 2)):
            if all(abs(float(medians[j]) - base) > 15 for j in (y, y + 1, y + 2)):
                return y
        binary = cv2.threshold(gray, 235, 255, cv2.THRESH_BINARY_INV)[1]
        horizontal = cv2.morphologyEx(
            binary, cv2.MORPH_OPEN, np.ones((1, max(20, gray.shape[1] // 8)), np.uint8)
        )
        rules = [
            i
            for i, count in enumerate((horizontal > 0).sum(axis=1))
            if count > gray.shape[1] * 0.2 and i > 5
        ]
        if not rules:
            raise ReviewRequired(
                "TABLE_HEADER_UNAVAILABLE", "Cannot establish the current table header boundary"
            )
        return rules[0]

    @staticmethod
    def _empty_table(table) -> bool:
        import cv2
        import numpy as np

        image = table.capture_as_image().convert("RGB")
        # NatTable's first horizontal rule separates its header. Detect it in pixels;
        # no row height or screen location is persisted.
        gray = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2GRAY)
        binary = cv2.threshold(gray, 230, 255, cv2.THRESH_BINARY_INV)[1]
        horizontal = cv2.morphologyEx(
            binary, cv2.MORPH_OPEN, np.ones((1, max(20, image.width // 8)), np.uint8)
        )
        rules = [
            i
            for i, count in enumerate((horizontal > 0).sum(axis=1))
            if count > image.width * 0.2 and i > 5
        ]
        if not rules:
            return False
        body = binary[SemanticUI.header_height(gray) + 2 :, :]
        if not body.size:
            return False
        vertical = cv2.morphologyEx(
            body, cv2.MORPH_OPEN, np.ones((max(15, body.shape[0] // 3), 1), np.uint8)
        )
        horizontal = cv2.morphologyEx(
            body, cv2.MORPH_OPEN, np.ones((1, max(20, image.width // 8)), np.uint8)
        )
        residual = cv2.subtract(body, cv2.bitwise_or(vertical, horizontal))
        count, _, statistics, _ = cv2.connectedComponentsWithStats(residual)
        return not any(statistics[i, cv2.CC_STAT_AREA] > 8 for i in range(1, count))

    def selected_row_number(self, table):
        import cv2
        import numpy as np
        import pytesseract
        from PIL import Image, ImageOps

        from .ocr import executable

        image = table.capture_as_image().convert("L")
        _, y = self.row_point(table)
        height = self.header_height(np.array(image))
        width = self._row_header_width.get(table.handle, 0)
        if not 0 < width < image.width * 0.15:
            raise ReviewRequired(
                "ROW_NUMBER_COLUMN_UNAVAILABLE", "Cannot establish the row-number column"
            )
        crop = np.array(image.crop((1, y - height // 2 + 1, width - 1, y + height // 2)))
        binary = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
        if float(np.median(binary)) < 128:
            binary = cv2.bitwise_not(binary)
        fingerprint = hashlib.sha256(str(binary.shape).encode() + binary.tobytes()).hexdigest()
        if fingerprint not in self._row_number_cache:
            enlarged = ImageOps.expand(
                Image.fromarray(binary).resize((binary.shape[1] * 4, binary.shape[0] * 4)),
                border=12,
                fill="white",
            )
            pytesseract.pytesseract.tesseract_cmd = executable(self.settings)
            for psm in (7, 8):
                text = pytesseract.image_to_string(
                    enlarged,
                    lang="eng",
                    config=f"--psm {psm} -c tessedit_char_whitelist=0123456789",
                    timeout=10,
                ).strip()
                if re.fullmatch(r"[1-9]\d{0,3}", text) and int(text) <= 1000:
                    self._row_number_cache[fingerprint] = int(text)
                    break
            else:
                raise ReviewRequired(
                    "ROW_NUMBER_UNREADABLE", "Cannot read the selected list-row number"
                )
        return self._row_number_cache[fingerprint]

    def copy_list_rows(self, scope):
        """Establish completeness even when a selector copies only one row."""
        headers, initial = self.copy_rows(scope)
        if not initial:
            return headers, initial
        table = self.table(scope)
        self.focus_table(table)
        table.type_keys("^{END}", set_foreground=False)
        total = self.selected_row_number(table)
        last_headers, last = self.copy_rows(scope, current_selection=True, known_headers=headers)
        if last_headers != headers or len(last) != 1:
            raise ReviewRequired(
                "LIST_END_UNCONFIRMED", "The last selected list row was not copied uniquely"
            )
        if len(initial) == total and initial[-1] == last[0]:
            return headers, initial
        table.type_keys("^{HOME}", set_foreground=False)
        result = []
        for index in range(1, total + 1):
            if index > 1:
                table.type_keys("{DOWN}", set_foreground=False)
            self.wait(
                lambda: self.selected_row_number(table),
                lambda value, expected=index: value == expected,
                timeout=3,
            )
            current_headers, current = self.copy_rows(
                scope, current_selection=True, known_headers=headers
            )
            if self.selected_row_number(table) != index:
                raise ReviewRequired(
                    "LIST_SELECTION_CHANGED", "List selection changed while copying a row"
                )
            if current_headers != headers or len(current) != 1:
                raise ReviewRequired(
                    "LIST_ROW_UNCONFIRMED", "A selected list row was not copied uniquely"
                )
            result.extend(current)
        if result[-1] != last[0]:
            raise ReviewRequired("LIST_CHANGED_DURING_READ", "List end changed during enumeration")
        return headers, result

    def rows(self, scope) -> list[dict]:
        reader = (
            self.copy_list_rows
            if key(scope.window_text())
            in {
                "selecttheaddress",
            }
            else self.copy_rows
        )
        headers, rows = reader(scope)
        return [
            {
                key(header) or f"column{i}": value
                for i, (header, value) in enumerate(zip(headers, row, strict=False))
            }
            for row in rows
        ]

    def open_row(self, scope, index, *, column=None):
        headers = self.copy_rows(scope)[0] if column else []
        table = self.select_row(scope, index)
        x, y = self.row_point(table)
        if column:
            self._headers(table, len(headers), scan=False)
            point = self._header_cells.get(table.handle, {}).get(key(column))
            if point is None:
                raise ReviewRequired(
                    "ROW_OPEN_COLUMN_UNAVAILABLE", f"Cannot observe {column!r} in the current table"
                )
            x = point[0]
        table.double_click_input(coords=(x, y))

    def select_row(self, scope, index):
        table = self.table(scope)
        self.focus_table(table)
        table.type_keys("^{HOME}", set_foreground=False)
        first = self.row_point(table, first=True)
        table.click_input(coords=first)
        table.type_keys("{DOWN}" * index, set_foreground=False)
        self.row_point(table)  # Require a uniquely visible selected row.
        return table

    @staticmethod
    def row_point(table, *, first=False):
        import cv2
        import numpy as np

        rgb = np.array(table.capture_as_image().convert("RGB"))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        binary = cv2.threshold(gray, 235, 255, cv2.THRESH_BINARY_INV)[1]
        header = SemanticUI.header_height(gray)
        # The tested NatTable profile uses equal header/body row heights. Selected
        # blue rows are solid pixel bands and cannot be treated as grid rulings.
        row_height = header + 1
        y = header + row_height // 2
        if not first:
            colors = rgb.astype(float)
            blue = (colors[:, :, 2] - colors[:, :, 0] > 12) & (
                colors[:, :, 1] - colors[:, :, 0] > 3
            )
            selected = [
                i
                for i, count in enumerate(blue.sum(axis=1))
                if count > rgb.shape[1] * 0.25 and i > header
            ]
            if not selected or selected[-1] - selected[0] > row_height * 1.5:
                raise ReviewRequired(
                    "ROW_SELECTION_UNCONFIRMED", "A unique selected row is not visible"
                )
            y = (selected[0] + selected[-1]) // 2
        vertical = cv2.morphologyEx(
            binary, cv2.MORPH_OPEN, np.ones((max(20, rgb.shape[0] // 3), 1), np.uint8)
        )
        columns = [
            i
            for i, count in enumerate((vertical > 0).sum(axis=0))
            if count > rgb.shape[0] * 0.5 and i > 5
        ]
        return (columns[0] // 2 if columns else rgb.shape[1] // 2, y)

    def discard_current_draft(self, expected_name):
        self.activate_editor()
        if key(self.editor_stack().window_text()) != key(expected_name):
            raise ReviewRequired(
                "DRAFT_DISCARD_AMBIGUOUS", "The selected editor is not the expected owned draft"
            )
        self.root().type_keys("^w", set_foreground=False)
        dialog = self.dialog("Save Parts")
        listing = self.one(dialog, "Select the parts to save:", "List")
        native = self.native_control(listing)
        if native.item_count() != 1 or native.get_item(0).text() != expected_name:
            self.click(dialog, "Cancel")
            raise ReviewRequired(
                "DRAFT_DISCARD_AMBIGUOUS", "Save dialog does not identify exactly the owned draft"
            )
        if native.get_item(0).is_checked():
            listing.set_focus()
            listing.type_keys("{HOME}{SPACE}", set_foreground=False)
        self.wait(lambda: native.get_item(0).is_checked(), lambda checked: not checked)
        self.click(dialog, "OK")
        self.wait(lambda: self.controls(self.root(), "Window"), lambda windows: not windows)

    def icon(self, scope, resource: str):
        self.activate()
        import numpy as np
        from PIL import Image

        if self.app_root is None:
            raise ReviewRequired("ICONS_UNAVAILABLE", "Fakturama installation path is unknown")
        archives = list(
            (self.app_root / "plugins").glob("com.sebulli.fakturama.resources.icons_*.jar")
        )
        if len(archives) != 1:
            raise ReviewRequired("ICONS_AMBIGUOUS", "Expected one installed icon resource bundle")
        with zipfile.ZipFile(archives[0]) as archive:
            reference = Image.open(io.BytesIO(archive.read(resource))).convert("RGBA")
        scores = []
        for control in self.native_control(scope).descendants(class_name="Static"):
            rect = control.rectangle()
            if (
                control.window_text()
                or not control.is_visible()
                or not 8 <= rect.width() <= 80
                or not 8 <= rect.height() <= 80
            ):
                continue
            observed = control.capture_as_image().convert("RGB")
            resized = reference.resize(observed.size, Image.Resampling.BILINEAR)
            mask = np.array(resized)[:, :, 3] > 200
            if not mask.any():
                continue
            error = np.abs(
                np.asarray(observed, dtype=float) - np.asarray(resized.convert("RGB"), dtype=float)
            )[mask].mean()
            scores.append((1 - float(error) / 255, control))
        scores.sort(key=lambda item: item[0], reverse=True)
        if (
            not scores
            or scores[0][0] < 0.95
            or (len(scores) > 1 and scores[0][0] - scores[1][0] < 0.08)
        ):
            raise ReviewRequired("ICON_AMBIGUOUS", f"No unique current image match for {resource}")
        scores[0][1].click_input()
