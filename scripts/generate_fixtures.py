"""Generate deterministic synthetic documents and separate ground-truth files.

The supplied sample.json is a manual transcription of the VISIBLE source in the brief.
Synthetic recordings are labelled as such and must never be reported as API responses.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from image_to_cash.artifacts import write_json
from image_to_cash.models import ExtractionResult, FieldEvidence, OrderInput
from image_to_cash.validation import calculate, flatten, line_net

ROOT = Path(__file__).resolve().parents[1]


def font(size: int, bold=False):
    candidates = [
        Path("C:/Windows/Fonts") / ("arialbd.ttf" if bold else "arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu")
        / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
    ]
    return next(
        (ImageFont.truetype(str(path), size) for path in candidates if path.exists()),
        ImageFont.load_default(size=size),
    )


def render(order: OrderInput, path: Path, layout: int = 0) -> None:
    width, height = 1800, 1560 + len(order.items) * 70 + (240 if layout == 2 else 0)
    adjustments = (
        order.document_discount_percent,
        order.shipping_net,
        order.shipping_vat_percent,
    )
    if any(value is not None for value in adjustments):
        height += 110
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    ink, muted = "#182b43", "#53637a"
    accent = ["#18334c", "#275b50", "#4d4265"][layout]
    draw.rectangle((0, 0, width, 100), fill=accent)
    draw.text(
        (65, 28), "SALES ORDER INPUT  |  SYNTHETIC TEST DATA", font=font(33, True), fill="white"
    )
    positions = [70, 640, 1210]

    def cell(label, value, x, y):
        draw.text((x, y), label, font=font(23, True), fill=muted)
        draw.text((x, y + 36), str(value or ""), font=font(29), fill=ink)

    # Layout 1 reverses labelled columns. Layout 2 stacks addresses vertically.
    # Table column order remains the printed brief's order in every layout.
    offset = [0, 18, 36][layout]
    y = 145
    cell("EXTERNAL REFERENCE", order.external_reference, 70, y)
    cell("ORDER DATE", order.order_date, 680 + offset, y)
    cell("CURRENCY", order.currency, 1300, y)
    y += 110
    cell("CUSTOMER ID", order.customer.source_customer_id, 950 if layout == 1 else 70, y)
    cell("COMPANY", order.customer.company, 70 if layout == 1 else 680 + offset, y)
    y += 110
    cell(
        "CONTACT NAME",
        f"{order.customer.first_name or ''} {order.customer.last_name or ''}".strip(),
        950 if layout == 1 else 70,
        y,
    )
    cell("CUSTOMER ALIAS", order.customer.alias, 70 if layout == 1 else 680 + offset, y)
    y += 110
    cell("EMAIL", order.customer.email, 70, y)
    cell("PHONE", order.customer.telephone, 950, y)
    y += 120
    for index, (label, address) in enumerate(
        [("BILLING ADDRESS", order.customer.billing), ("DELIVERY ADDRESS", order.customer.delivery)]
    ):
        x = (
            (950 if index == 0 else 70)
            if layout == 1
            else (70 if index == 0 or layout == 2 else 950)
        )
        address_y = y + (240 * index if layout == 2 else 0)
        draw.text((x, address_y), label, font=font(23, True), fill=muted)
        lines = [
            address.recipient,
            address.additional_name,
            address.street,
            f"{address.postcode or ''} {address.city or ''}".strip(),
            address.country,
        ]
        for index, value in enumerate(v for v in lines if v):
            draw.text((x, address_y + 38 + index * 39), value, font=font(29), fill=ink)
    y += 510 if layout == 2 else 270
    payment_positions = [1210, 70, 640] if layout == 1 else positions
    cell("PAYMENT METHOD", order.payment.method, payment_positions[0], y)
    cell("PAID STATUS", order.payment.status, payment_positions[1], y)
    cell("PAYMENT DATE", order.payment.payment_date, payment_positions[2], y)
    y += 115
    draw.text((70, y), "ITEMS", font=font(25, True), fill=accent)
    y += 48
    columns = [70, 305, 920, 1050, 1170, 1375, 1510, 1630]
    headers = ["SKU", "Description", "Qty", "Unit", "Unit net", "Disc.", "VAT", "Line net"]
    draw.rectangle((50, y - 10, 1770, y + 50), fill=accent)
    for x, heading in zip(columns, headers, strict=False):
        draw.text((x, y), heading, font=font(23, True), fill="white")
    y += 65
    for index, item in enumerate(order.items):
        if index % 2 == 0:
            draw.rectangle((50, y - 8, 1770, y + 54), fill="#eef3f8")
        values = [
            item.sku,
            item.description,
            str(item.quantity),
            item.unit,
            format(item.unit_net, ".2f"),
            f"{item.discount_percent}%",
            f"{item.vat_percent}%",
            format(item.printed_net, ".2f"),
        ]
        for x, value in zip(columns, values, strict=False):
            draw.text((x, y), value or "", font=font(26), fill=ink)
        y += 70
    y += 35
    if any(value is not None for value in adjustments):
        for x, label, value in zip(
            positions,
            ["DOCUMENT DISCOUNT", "SHIPPING NET", "SHIPPING VAT"],
            adjustments,
            strict=True,
        ):
            if value is not None:
                cell(label, f"{value}%" if label != "SHIPPING NET" else f"EUR {value:.2f}", x, y)
        y += 110
    for x, label, amount in zip(
        positions,
        ["NET TOTAL", "VAT TOTAL", "GROSS TOTAL"],
        [order.totals.net, order.totals.vat, order.totals.gross],
        strict=False,
    ):
        cell(label, f"EUR {amount:.2f}" if amount is not None else "", x, y)
    draw.text(
        (70, height - 55),
        "Synthetic data | Net prices | Dates YYYY-MM-DD | EUR",
        font=font(22),
        fill=muted,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def recording(order: OrderInput, origin: str) -> dict:
    evidence = [
        FieldEvidence(field=path, raw_text=str(value), engine="reviewed-fixture")
        for path, value in flatten(order).items()
        if value is not None
    ]
    return {
        "origin": origin,
        "extraction": ExtractionResult(order=order, evidence=evidence).model_dump(mode="json"),
    }


def main():
    base = OrderInput.model_validate_json(
        (ROOT / "fixtures/sample.json").read_text(encoding="utf-8")
    )
    write_json(
        ROOT / "fixtures/sample.reviewed.json",
        recording(base, "Manual transcription of visible PDF page 3; NOT an API recording"),
    )
    render(base, ROOT / "fixtures/sample.synthetic.png")
    manifest = []
    for index in range(24):
        order = base.model_copy(deep=True)
        order.external_reference = f"SYN-2026-{index + 1:04d}"
        order.order_date = date(2026, 7, 1) + timedelta(days=index)
        order.customer.company = f"Example Office {index + 1:02d} GmbH"
        order.customer.first_name, order.customer.last_name = "Ada", "Weber"
        order.customer.alias = f"EXAMPLE-{index + 1:02d}"
        order.customer.email = f"orders{index + 1:02d}@example.test"
        order.customer.source_customer_id = f"SRC-{index + 1:04d}"
        order.customer.billing.recipient = order.customer.company
        order.customer.billing.postcode = f"0{1000 + index}"
        order.customer.delivery.recipient = f"Example Warehouse {index + 1:02d}"
        order.payment.status = "UNPAID" if index % 3 == 0 else "PAID"
        order.payment.payment_date = (
            None if order.payment.status == "UNPAID" else order.order_date + timedelta(days=3)
        )
        order.payment.method = ["Bank Transfer", "Credit Card", "SEPA Direct Debit"][index % 3]
        for row, item in enumerate(order.items):
            item.sku = f"SKU-{index + 1:02d}-{row + 1:02d}"
            item.description = ["Office Chair", "Desk Mat"][row]
            item.quantity = Decimal(str(1 + (index + row) % 5))
            item.unit_net = Decimal("19.95") + Decimal(index * 3 + row)
            item.discount_percent = Decimal([0, 5, 10][(index + row) % 3])
            item.vat_percent = Decimal(7 if index % 4 == 0 and row == 1 else 19)
            item.printed_net = line_net(item)
        order.totals = calculate(order)
        split = "development" if index < 12 else "heldout"
        folder = ROOT / "fixtures/evaluation" / split
        stem = f"order-{index + 1:02d}"
        write_json(folder / f"{stem}.json", order.model_dump(mode="json"))
        render(order, folder / f"{stem}.png", index % 3)
        manifest.append(
            {
                "id": stem,
                "split": split,
                "layout": index % 3,
                "image": f"{split}/{stem}.png",
                "truth": f"{split}/{stem}.json",
                "synthetic": True,
            }
        )
    write_json(
        ROOT / "fixtures/evaluation/manifest.json",
        {
            "ground_truth": "Generated from explicit source data; visual review status is separate",
            "human_visual_review": "pending",
            "documents": manifest,
        },
    )
    print(
        "Generated 24 distinct synthetic documents (12 development / 12 heldout), sample reconstruction and labelled reviewed input"
    )


if __name__ == "__main__":
    main()
