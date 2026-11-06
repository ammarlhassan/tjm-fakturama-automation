"""Annotate the final official run's screenshots with vector overlays; PNGs stay unmodified."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from jinja2 import Environment, select_autoescape
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
RUN = "official-sample-live"
RECORDED = "Recorded during the run."
AUDIT = "Later read-only audit of the saved record, not a recording of its creation."
STEPS = [
    {
        "title": "1. Extract the unchanged official image",
        "brief": "Brief steps 1.1-1.2",
        "path": f"{RUN}/source-evidence.png",
        "scope": "The supplied image is the only input. Green boxes are local OCR locations; a box shows where text is, not that it is correct.",
        "notes": [
            (90, 296, 396, 28, "External reference WEB-2026-0714-A17 and Order Date 2026-07-14."),
            (
                602,
                517,
                243,
                28,
                "The source email uses the reserved .test domain. It stays unchanged in the source record.",
            ),
            (
                88,
                964,
                786,
                30,
                "Bank Transfer, literal PAID and payment date 2026-07-18.",
            ),
            (80, 1150, 968, 96, "Both item rows, including the 10% line discount on the chair."),
            (175, 1453, 806, 34, "Printed totals: EUR 570.00 net, 108.30 VAT, 678.30 gross."),
        ],
    },
    {
        "title": "2. Debtor created: the source email did not persist",
        "brief": "Brief steps 2.5-2.8, 2.13",
        "path": f"{RUN}/saved-contact.png",
        "scope": RECORDED
        + " The saved Debtor was closed and reopened, so these are persisted values.",
        "notes": [
            (462, 188, 310, 26, "Fakturama's proposed Customer ID is kept."),
            (
                412,
                695,
                755,
                120,
                "Main address holds the billing Street, ZIP, City and Country.",
            ),
            (412, 852, 756, 26, "Main address has the Invoice address role."),
            (
                1202,
                695,
                678,
                26,
                "E-Mail is blank. Fakturama 2.2.0 did not store marta.klein@example.test. The receipt records SOURCE_EMAIL_NOT_PERSISTED and does not claim full source agreement.",
            ),
            (1202, 726, 678, 26, "Telephone persisted as printed."),
            (
                522,
                628,
                172,
                26,
                "The different delivery address has its own tab with the Delivery address role.",
                "right",
            ),
        ],
    },
    {
        "title": "3. Debtor settings: alias, discount, Net and payment",
        "brief": "Brief steps 2.9-2.10",
        "path": f"{RUN}/later-audit/audit-contact-miscellaneous.png",
        "scope": AUDIT,
        "notes": [
            (472, 480, 686, 26, "Alias name from the source."),
            (1236, 513, 650, 26, "The created Bank Transfer method is selected."),
            (1236, 546, 650, 26, "Discount is 0%."),
            (465, 579, 693, 26, "Net or Gross is Net."),
        ],
    },
    {
        "title": "4. Payment method created with the mapped code",
        "brief": "Brief steps 2.10.3-2.10.6",
        "path": f"{RUN}/later-audit/audit-payment.png",
        "scope": AUDIT,
        "notes": [
            (517, 198, 1402, 86, "Name and Description are Bank Transfer; Account is blank."),
            (398, 291, 1521, 26, "Bank Transfer maps to the Credit transfer code."),
            (498, 323, 1421, 87, "Cash discount, Discount Days and Net Days are 0."),
            (528, 677, 410, 28, "The standard method is still Cash; Set as standard was not used."),
        ],
    },
    {
        "title": "5. VAT rate created before the Product",
        "brief": "Brief steps 3.4-3.7",
        "path": f"{RUN}/later-audit/audit-vat-19.png",
        "scope": AUDIT,
        "notes": [
            (458, 198, 1462, 86, "Name and Description are VAT 19%."),
            (398, 291, 1522, 26, "VAT code is S (Standard rate)."),
            (494, 323, 1426, 26, "Value is 19%."),
            (474, 358, 408, 26, "The standard VAT is still Tax-free."),
        ],
    },
    {
        "title": "6. Product created with its undiscounted master price",
        "brief": "Brief steps 3.8-3.11",
        "path": f"{RUN}/later-audit/audit-product-CHR-ERG-01.png",
        "scope": AUDIT + " The second Product, MAT-DESK-02, is in the same audit folder.",
        "notes": [
            (446, 203, 820, 56, "Item Number is the SKU; Name comes from the item description."),
            (
                452,
                448,
                246,
                26,
                "Gross master price 297.50 is 250.00 x 1.19, with no line discount applied.",
            ),
            (434, 479, 244, 26, "Cost price is 0.00."),
            (505, 510, 144, 26, "The exact VAT 19% is selected."),
            (493, 543, 773, 26, "Stock is 0.00."),
        ],
    },
    {
        "title": "7. Order complete before Save",
        "brief": "Brief steps 1.3-1.7, 3.13-3.17, 4.1-4.3",
        "path": f"{RUN}/order-before-save.png",
        "scope": RECORDED + " An editor view does not prove persistence; step 9 does.",
        "notes": [
            (404, 178, 660, 30, "Generated No. PO000001 is kept; Date is the source Order Date."),
            (408, 254, 565, 26, "Cust.Ref. is the extracted External Reference."),
            (1086, 180, 310, 26, "Document mode is Net; VAT below is With VAT."),
            (
                441,
                420,
                1178,
                76,
                "Both source rows. Chair: quantity 2, U.Price 250.00, 10% discount, line 450.00. Mat: quantity 3, U.Price 40.00, line 120.00.",
            ),
            (1490, 826, 410, 150, "570,00 + 108,30 = 678,30, with 0% discount and free shipping."),
        ],
    },
    {
        "title": "8. Linked Invoice with the source payment",
        "brief": "Brief steps 4.6-4.7, 5.1-5.3",
        "path": f"{RUN}/invoice-before-save.png",
        "scope": RECORDED + " Created from the saved Order's follow-up action.",
        "notes": [
            (404, 178, 660, 30, "Generated Invoice No. and Invoice Date are kept."),
            (988, 302, 428, 66, "Service date stays generated; Order Date copied from the Order."),
            (476, 312, 500, 104, "Delivery address copied; the Invoice address tab is also read."),
            (
                395,
                925,
                455,
                28,
                "Literal PAID: Bank Transfer, source date 18 Jul 2026, full value 678,30.",
            ),
        ],
    },
    {
        "title": "9. Saved records: paid Invoice, open source Order",
        "brief": "Brief steps 4.5, 5.5-5.7",
        "path": f"{RUN}/final-verification.png",
        "scope": RECORDED + " The Order was reopened from its saved row before this capture.",
        "notes": [
            (
                423,
                750,
                130,
                22,
                "This transaction is selected with a blank search, so the application's own grouping links the pair.",
            ),
            (756, 814, 332, 50, "Exactly one Invoice and one Order: INV000001 and PO000001."),
            (
                1423,
                814,
                333,
                50,
                "Invoice is paid; source Order stays open; both total 678,30.",
                "right",
            ),
            (1437, 340, 300, 54, "No Delivery, Correction or Dunning document was created."),
        ],
    },
]


def main():
    receipt = json.loads((ARTIFACTS / RUN / "receipt.json").read_text(encoding="utf-8"))
    verification = json.loads((ARTIFACTS / RUN / "verification.json").read_text(encoding="utf-8"))
    if verification["reused_provider_responses"]:
        raise RuntimeError(f"{RUN} reused cached provider responses; it is not a live run")
    fresh = verification["fresh_provider_calls"]
    summary = (
        f"Unchanged official image. Local OCR and {fresh} fresh Gemini "
        f"{'call' if fresh == 1 else 'calls'}. Brand-new Fakturama workspace. "
        f"{verification['operator_reviews']} operator reviews and "
        f"{verification['provider_retries']} provider retries. "
        f"The reserved .test email did not persist and is flagged."
    )
    steps = []
    for step in STEPS:
        path = ARTIFACTS / step["path"]
        with Image.open(path) as image:
            width, height = image.size
        notes = []
        for x, y, w, h, text, *side in step["notes"]:
            if x < 0 or y < 0 or x + w > width or y + h > height:
                raise RuntimeError(f"Annotation outside {step['path']}: {(x, y, w, h)}")
            # The marker sits beside the box, so it never covers a label or value inside it.
            notes.append(
                {
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "text": text,
                    "right": bool(side),
                    "cy": y + h // 2,
                }
            )
        steps.append(
            {
                **step,
                "notes": notes,
                "width": width,
                "height": height,
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        )
    template = Environment(
        autoescape=select_autoescape(default=True)
    ).from_string("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TJM annotated evidence</title>
<style>body{margin:0;background:#f2f5f9;color:#172c43;font:16px/1.6 system-ui}header{background:#14263c;color:white;padding:36px max(24px,calc((100vw - 1280px)/2));border-bottom:5px solid #36c3ad}header small{color:#8ee2d2;letter-spacing:2px}h1{font-size:34px;margin:8px 0}main{max-width:1280px;margin:26px auto;padding:0 24px}section,.notice{background:white;border:1px solid #dbe4ed;border-radius:10px;padding:22px;margin-bottom:22px}.notice{border-left:5px solid #d58a20}a{color:#17675c}nav{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0}button{border:1px solid #8ca6b5;background:white;padding:9px 13px;border-radius:7px;font:14px system-ui;cursor:pointer}button[aria-selected=true]{background:#17675c;color:white}svg{width:100%;height:auto;border:1px solid #b7c6d3}svg rect.marker{fill:transparent;stroke:#e17b12;stroke-width:4}svg circle{fill:#14263c;stroke:white;stroke-width:2}svg text{fill:white;font:bold 22px Arial;text-anchor:middle;dominant-baseline:central}svg a:hover rect{stroke:#13ad90;stroke-width:7}li{margin:8px 0}.muted{color:#53657d;font-size:14px}pre{background:#edf2f7;padding:16px;overflow:auto;font-size:12px}[hidden]{display:none}@media print{nav{display:none}[hidden]{display:block}section{break-before:page}}</style></head>
<body><header><small>TJM LABS · REAL DESKTOP EVIDENCE</small><h1>The official order image to verified saved records</h1><p>Annotated screenshots · Order {{ receipt.order_number }} · Invoice {{ receipt.invoice_number }} · Debtor {{ receipt.debtor_number }}</p></header>
<main><div class="notice"><strong>{{ summary }}</strong> Identity, addresses, items, totals, dates, payment and the document relationship were all checked strictly; complete source-field agreement is not claimed.</div>
<p><a href="{{ run }}/report.html">Complete run report</a> · <a href="{{ run }}/receipt.json">JSON receipt</a> · <a href="{{ run }}/verification.json">Acceptance checks</a> · <a href="{{ run }}/later-audit/result.json">Read-only audit</a> · <a href="submission-evidence.pdf">Download annotated PDF</a></p>
<nav aria-label="Evidence steps">{% for step in steps %}<button id="tab-{{ loop.index0 }}" type="button" onclick="show({{ loop.index0 }})" aria-controls="step-{{ loop.index0 }}" aria-selected="{{ 'true' if loop.first else 'false' }}">{{ step.title }}</button>{% endfor %}</nav>
{% for step in steps %}{% set outer=loop.index0 %}<section id="step-{{ outer }}" aria-labelledby="tab-{{ outer }}"><h2>{{ step.title }}</h2><p class="muted">{{ step.brief }}. {{ step.scope }}</p><svg viewBox="0 0 {{ step.width }} {{ step.height }}" role="img" aria-label="{{ step.title }}: recorded screenshot with numbered annotations"><image href="data:image/png;base64,{{ step.data }}" width="{{ step.width }}" height="{{ step.height }}"/>{% for n in step.notes %}{% set cx = n.x + n.w + 20 if n.right else n.x - 20 %}<a href="#note-{{ outer }}-{{ loop.index }}"><title>{{ n.text }}</title><rect class="marker" x="{{ n.x }}" y="{{ n.y }}" width="{{ n.w }}" height="{{ n.h }}"/><circle cx="{{ cx }}" cy="{{ n.cy }}" r="17"/><text x="{{ cx }}" y="{{ n.cy }}">{{ loop.index }}</text></a>{% endfor %}</svg><ol>{% for n in step.notes %}<li id="note-{{ outer }}-{{ loop.index }}">{{ n.text }}</li>{% endfor %}</ol><p><a href="{{ step.path }}">Inspect the original unmodified image</a></p></section>{% endfor %}
<section><h2>Verified completion receipt</h2><details><summary>Show the receipt</summary><pre>{{ receipt | tojson(indent=2) }}</pre></details><p>See <a href="../docs/limitations.md">verification limits</a> and <a href="../docs/requirements-status.md">requirement status</a>.</p></section></main>
<script>function show(n){document.querySelectorAll('section[id^="step-"]').forEach((p,i)=>p.hidden=i!==n);document.querySelectorAll('nav button').forEach((b,i)=>b.setAttribute('aria-selected',String(i===n)))}show(0);</script></body></html>""")
    (ARTIFACTS / "submission-demo.html").write_text(
        template.render(steps=steps, receipt=receipt, summary=summary, run=RUN), encoding="utf-8"
    )

    pdf = canvas.Canvas(str(ARTIFACTS / "submission-evidence.pdf"), pagesize=landscape(A4))
    pdf.setTitle("TJM: annotated evidence for the official sample")
    width, height = landscape(A4)
    caption = ParagraphStyle(
        "caption",
        fontName="Helvetica",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#23364a"),
    )
    for step in steps:
        pdf.setFillColor(colors.HexColor("#14263c"))
        pdf.setFont("Helvetica-Bold", 15)
        pdf.drawString(34, height - 30, step["title"])
        scope = Paragraph(f"{step['brief']}. {step['scope']}", caption)
        _, scope_height = scope.wrap(width - 68, 40)
        scope.drawOn(pdf, 34, height - 40 - scope_height)
        scale = min((width - 68) / step["width"], 395 / step["height"])
        left, bottom = (width - step["width"] * scale) / 2, 108
        pdf.drawImage(
            ImageReader(ARTIFACTS / step["path"]),
            left,
            bottom,
            width=step["width"] * scale,
            height=step["height"] * scale,
        )
        for index, note in enumerate(step["notes"], 1):
            px = left + note["x"] * scale
            py = bottom + (step["height"] - note["y"] - note["h"]) * scale
            pdf.setStrokeColor(colors.HexColor("#e17b12"))
            pdf.setLineWidth(1.2)
            pdf.rect(px, py, note["w"] * scale, note["h"] * scale)
            cx = px + note["w"] * scale + 8 if note["right"] else px - 8
            cy = py + note["h"] * scale / 2
            pdf.setFillColor(colors.HexColor("#14263c"))
            pdf.circle(cx, cy, 6.5, stroke=0, fill=1)
            pdf.setFillColor(colors.white)
            pdf.setFont("Helvetica-Bold", 8)
            pdf.drawCentredString(cx, cy - 3, str(index))
        y = 102
        for index, note in enumerate(step["notes"], 1):
            paragraph = Paragraph(f"<b>{index}.</b> {note['text']}", caption)
            _, paragraph_height = paragraph.wrap(width - 68, 40)
            paragraph.drawOn(pdf, 34, y - paragraph_height)
            y -= paragraph_height + 1.5
        pdf.setFillColor(colors.HexColor("#53657d"))
        pdf.setFont("Helvetica", 7)
        pdf.drawString(34, 14, summary)
        pdf.showPage()
    pdf.save()
    print("Created artifacts/submission-demo.html and artifacts/submission-evidence.pdf")


if __name__ == "__main__":
    main()
