"""Answer Fakturama's first-launch dialog for a dedicated workspace.

Run after `itc launch --workspace NAME`. It only enters the working directory the launcher
printed, keeps the default database setting, and waits for the main window to show that path.
It does nothing when the dialog is absent, and it never changes an initialized workspace.
"""

from __future__ import annotations

import argparse
import json
import time

from pywinauto import Desktop

from image_to_cash.config import Settings
from image_to_cash.doctor import workspace_matches_title


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--timeout", type=float, default=180)
    options = parser.parse_args()
    expected = Settings().workspace_path(options.workspace) / "data"
    desktop = Desktop(backend="uia")
    deadline = time.monotonic() + options.timeout
    answered = False
    while time.monotonic() < deadline:
        windows = desktop.windows()
        mains = [w for w in windows if w.window_text().startswith("Fakturama -")]
        if mains:
            if len(mains) != 1 or not workspace_matches_title(mains[0].window_text(), expected):
                raise SystemExit(f"Unexpected Fakturama window: {mains[0].window_text()!r}")
            print(
                json.dumps(
                    {"ready": True, "answered_dialog": answered, "title": mains[0].window_text()}
                )
            )
            return
        dialogs = [
            w
            for w in windows
            if w.window_text() == "Fakturama Initialization" and w.class_name() == "SWT_Window0"
        ]
        notices = [
            notice
            for dialog in dialogs
            for notice in dialog.descendants(control_type="Window")
            if notice.window_text() == "Information"
        ]
        if notices:
            # Fakturama can announce a restart before it reopens on the chosen directory.
            text = " ".join(c.window_text() for c in notices[0].descendants(control_type="Text"))
            if "To switch the workspace" not in text:
                raise SystemExit(f"Unexpected first-launch notice: {text!r}")
            answered = True
            confirm = [
                c for c in notices[0].descendants(control_type="Button") if c.window_text() == "OK"
            ]
            confirm[0].invoke()
        elif dialogs and not answered:
            dialog = dialogs[0]
            fields = [
                c
                for c in dialog.descendants(control_type="Edit")
                if c.window_text() == "Working Directory"
            ]
            if len(fields) != 1:
                raise SystemExit("Cannot identify the Working Directory field")
            fields[0].set_edit_text(str(expected))
            check = [
                c
                for c in dialog.descendants(control_type="CheckBox")
                if c.window_text() == "use default database settings"
            ]
            if len(check) != 1 or not check[0].get_toggle_state():
                raise SystemExit("The default database setting is not selected")
            button = [
                c for c in dialog.descendants(control_type="Button") if c.window_text() == "OK"
            ]
            button[0].invoke()
            answered = True
        time.sleep(1)
    raise SystemExit("Fakturama did not reach its main window in time")


if __name__ == "__main__":
    main()
