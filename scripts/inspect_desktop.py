"""Capture only the Fakturama accessibility tree and its current window."""

import json
import sys
from pathlib import Path

from pywinauto import Desktop

target = Path(sys.argv[1])
target.mkdir(parents=True, exist_ok=True)
windows = [w for w in Desktop(backend="uia").windows() if "fakturama" in w.window_text().lower()]
print([(w.window_text(), w.process_id()) for w in windows])
for index, window in enumerate(windows):
    elements = []
    for item in [window, *window.descendants()]:
        try:
            info = item.element_info
            rect = item.rectangle()
            entry = {
                "name": info.name,
                "type": info.control_type,
                "id": info.automation_id,
                "class": info.class_name,
                "rect": [rect.left, rect.top, rect.right, rect.bottom],
                "visible": item.is_visible(),
                "enabled": item.is_enabled(),
            }
            try:
                entry["value"] = item.get_value()
            except Exception:
                pass
            elements.append(entry)
        except Exception:
            pass
    (target / f"window-{index}.json").write_text(
        json.dumps(elements, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    window.capture_as_image().save(target / f"window-{index}.png")
    print(f"{window.window_text()}: {len(elements)} elements")
