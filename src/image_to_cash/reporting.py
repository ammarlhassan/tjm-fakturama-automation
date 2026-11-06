from __future__ import annotations

import base64
import json
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image, ImageDraw

from .validation import flatten


def build_report(job: dict, events: list[dict], folder: Path) -> Path:
    source_image = Path(job["image_path"])
    image_data = ""
    if source_image.exists():
        with Image.open(source_image) as source:
            annotated = source.convert("RGB")
            draw = ImageDraw.Draw(annotated)
            extraction = job.get("extraction") or {}
            for evidence in (extraction.get("independent") or {}).get("evidence", []):
                for box in evidence.get("boxes", []):
                    draw.rectangle(
                        (box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]),
                        outline="#0f8e85",
                        width=2,
                    )
            annotated.save(folder / "source-evidence.png")
        image_data = base64.b64encode((folder / "source-evidence.png").read_bytes()).decode()
    extracted = flatten(job.get("source") or {})
    independent = flatten(
        ((job.get("extraction") or {}).get("independent") or {}).get("order") or {}
    )
    saved_path = folder / "observed-documents.json"
    saved = json.loads(saved_path.read_text(encoding="utf-8")) if saved_path.exists() else {}
    observed = flatten(saved.get("invoice") or {})
    warnings = (job.get("receipt") or {}).get("warnings") or []
    flagged = {warning["field"]: warning for warning in warnings}
    rows = []
    for field, value in extracted.items():
        if value is None:
            continue
        mapped = {"payment.method": "payment_method", "payment.payment_date": "payment_date"}.get(
            field, field
        )
        actual = observed.get(mapped)
        if field == "payment.status" and observed:
            actual = "PAID" if observed.get("paid") else "UNPAID"
        rows.append(
            {
                "field": field,
                "source": value,
                "ocr": independent.get(field),
                "saved": actual,
                "note": flagged[field]["message"]
                if field in flagged
                else "Retained as source evidence; Fakturama generates its Customer ID."
                if field == "customer.source_customer_id"
                else "Unit is retained in extraction; this application table does not expose a unit field."
                if field.startswith("items.") and field.endswith(".unit")
                else "",
            }
        )
    pending, timings = {}, []
    for event in events:
        if event["status"] == "intent":
            pending[event["action"]] = event
        elif event["status"] == "observed" and event["action"] in pending:
            before = pending.pop(event["action"])
            seconds = (
                datetime.fromisoformat(event["timestamp"])
                - datetime.fromisoformat(before["timestamp"])
            ).total_seconds()
            timings.append(
                {
                    "action": event["action"],
                    "seconds": round(seconds, 3),
                    "intent_event": before["id"],
                    "observed_event": event["id"],
                }
            )
    attempts = (job.get("extraction") or {}).get("attempts") or []
    screenshots = []
    for path in sorted(folder.glob("*.png")):
        if path.name != "source-evidence.png":
            screenshots.append(
                {"name": path.name, "data": base64.b64encode(path.read_bytes()).decode()}
            )
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    html = environment.get_template("report.html").render(
        job=job,
        events=events,
        rows=rows,
        image_data=image_data,
        attempts=attempts,
        screenshots=screenshots,
        saved=saved,
        timings=timings,
        warnings=warnings,
        operator_reviews=sum(event["action"] == "human_review" for event in events),
        extraction_retries=sum(event["action"] == "technical_review" for event in events),
    )
    target = folder / "report.html"
    target.write_text(html, encoding="utf-8")
    return target
