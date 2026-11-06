"""Local text recognition and a conservative label/row parser independent of vision."""

from __future__ import annotations

import re
import shutil
import threading
import time
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from PIL import Image
from pydantic import Field

from .config import Settings
from .models import (
    Address,
    Box,
    Contract,
    Customer,
    ExtractionResult,
    FieldEvidence,
    ItemInput,
    OrderInput,
    PaymentInput,
    Totals,
)
from .validation import normalize

OCR_VERSION = "labels-rows-v3"
_rapid_lock = threading.Lock()


@lru_cache(maxsize=1)
def _rapid_reader():
    from rapidocr import RapidOCR

    return RapidOCR()


class OCRWord(Contract):
    text: str
    box: Box
    confidence: float
    line: str


class OCRDocument(Contract):
    engine: str
    version: str
    width: int
    height: int
    words: list[OCRWord] = Field(default_factory=list)
    elapsed_seconds: float

    def lines(self) -> list[tuple[str, list[OCRWord]]]:
        # Engine block IDs split a table row into columns. Reconstruct physical rows
        # from vertical overlap, retaining the observed x order rather than block order.
        groups: list[list[OCRWord]] = []
        for word in sorted(self.words, key=lambda w: (w.box.y + w.box.height / 2, w.box.x)):
            matching = None
            for group in reversed(groups[-4:]):
                representative = group[0].box
                overlap = min(
                    representative.y + representative.height, word.box.y + word.box.height
                ) - max(representative.y, word.box.y)
                if overlap >= min(representative.height, word.box.height) * 0.35:
                    matching = group
                    break
            if matching is None:
                groups.append([word])
            else:
                matching.append(word)
        groups = [sorted(group, key=lambda w: w.box.x) for group in groups]
        return [(" ".join(w.text for w in words), words) for words in groups]


def executable(settings: Settings) -> str:
    found = shutil.which(settings.tesseract)
    if found:
        return found
    candidates = [
        settings.runtime / "tools" / "tesseract" / "tesseract.exe",
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("Tesseract not found; configure ITC_TESSERACT or run Windows setup")


def recognize(
    path: Path, settings: Settings, *, engine: str | None = None, psm: int = 6
) -> OCRDocument:
    started = time.perf_counter()
    engine = engine or settings.ocr_engine
    with Image.open(path) as image:
        width, height = image.size
        if engine == "tesseract":
            import math

            import cv2
            import numpy as np
            import pytesseract

            pytesseract.pytesseract.tesseract_cmd = executable(settings)
            scale = max(1.0, min(2.0, 3000 / width, math.sqrt(15_000_000 / (width * height))))
            pixels = np.array(image.convert("RGB"))
            gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
            binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
            horizontal = cv2.morphologyEx(
                binary, cv2.MORPH_OPEN, np.ones((1, max(40, width // 10)), np.uint8)
            )
            vertical = cv2.morphologyEx(
                binary, cv2.MORPH_OPEN, np.ones((max(40, height // 20), 1), np.uint8)
            )
            pixels[cv2.bitwise_or(horizontal, vertical) > 0] = 255
            processed = Image.fromarray(pixels).resize(
                (round(width * scale), round(height * scale)), Image.Resampling.LANCZOS
            )
            data = pytesseract.image_to_data(
                processed,
                lang=settings.languages,
                config=f"--oem 1 --psm {psm}",
                output_type=pytesseract.Output.DICT,
                timeout=60,
            )
            words = []
            for i, text in enumerate(data["text"]):
                if text.strip() and float(data["conf"][i]) >= 0:
                    words.append(
                        OCRWord(
                            text=text,
                            box=Box(
                                x=round(data["left"][i] / scale),
                                y=round(data["top"][i] / scale),
                                width=max(1, round(data["width"][i] / scale)),
                                height=max(1, round(data["height"][i] / scale)),
                            ),
                            confidence=float(data["conf"][i]) / 100,
                            line=f"{data['block_num'][i]}:{data['par_num'][i]}:{data['line_num'][i]}",
                        )
                    )
            version = str(pytesseract.get_tesseract_version())
        elif engine == "rapidocr":
            from importlib.metadata import version as package_version

            with _rapid_lock:
                result = _rapid_reader()(str(path))
            words = []
            if result.txts is not None:
                for i, (text, polygon, score) in enumerate(
                    zip(result.txts, result.boxes, result.scores, strict=False)
                ):
                    xs, ys = polygon[:, 0], polygon[:, 1]
                    words.append(
                        OCRWord(
                            text=text,
                            box=Box(
                                x=max(0, int(min(xs))),
                                y=max(0, int(min(ys))),
                                width=max(1, int(max(xs) - min(xs))),
                                height=max(1, int(max(ys) - min(ys))),
                            ),
                            confidence=float(score),
                            line=str(i),
                        )
                    )
            version = package_version("rapidocr")
        else:
            raise ValueError(f"Unknown OCR engine: {engine}")
    return OCRDocument(
        engine=engine,
        version=version,
        width=width,
        height=height,
        words=words,
        elapsed_seconds=time.perf_counter() - started,
    )


def number(raw: str) -> Decimal:
    cleaned = re.sub(r"(?:EUR|€|%)|\s", "", raw).strip()
    if "," in cleaned and "." in cleaned:
        cleaned = (
            cleaned.replace(".", "").replace(",", ".")
            if cleaned.rfind(",") > cleaned.rfind(".")
            else cleaned.replace(",", "")
        )
    elif "," in cleaned:
        if len(cleaned.rsplit(",", 1)[1]) == 3:
            raise ValueError("Ambiguous comma separator")
        cleaned = cleaned.replace(",", ".")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
        raise ValueError(f"Unreadable decimal: {raw}")
    return Decimal(cleaned)


LABELS = {
    "external_reference": ("EXTERNAL REFERENCE", "ORDER REFERENCE"),
    "order_date": ("ORDER DATE",),
    "source_customer_id": ("CUSTOMER ID",),
    "currency": ("CURRENCY",),
    "company": ("COMPANY",),
    "contact": ("CONTACT NAME",),
    "alias": ("CUSTOMER ALIAS", "ALIAS"),
    "email": ("EMAIL", "E-MAIL"),
    "telephone": ("PHONE", "TELEPHONE"),
    "billing": ("BILLING ADDRESS", "INVOICE ADDRESS"),
    "delivery": ("DELIVERY ADDRESS", "SHIPPING ADDRESS"),
    "method": ("PAYMENT METHOD",),
    "status": ("PAID STATUS", "PAYMENT STATUS"),
    "payment_date": ("PAYMENT DATE",),
    "net": ("NET TOTAL", "TOTAL NET"),
    "vat": ("VAT TOTAL", "TOTAL VAT"),
    "gross": ("GROSS TOTAL", "TOTAL GROSS"),
    "items": ("ITEMS", "ORDER ITEMS"),
    "document_discount_percent": ("DOCUMENT DISCOUNT",),
    "shipping_net": ("SHIPPING NET",),
    "shipping_vat_percent": ("SHIPPING VAT",),
}


def _label_key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


class LayoutParser:
    def __init__(self, document: OCRDocument):
        self.doc = document
        self.anchors: dict[str, tuple[Box, list[OCRWord]]] = {}
        self.evidence: list[FieldEvidence] = []
        self.issues: list[str] = []
        for _, words in document.lines():
            for name, alternatives in LABELS.items():
                for label in alternatives:
                    target = _label_key(label)
                    for start in range(len(words)):
                        for stop in range(start + 1, min(start + 5, len(words) + 1)):
                            chunk = words[start:stop]
                            if _label_key(" ".join(w.text for w in chunk)) == target:
                                x, y = min(w.box.x for w in chunk), min(w.box.y for w in chunk)
                                box = Box(
                                    x=x,
                                    y=y,
                                    width=max(w.box.x + w.box.width for w in chunk) - x,
                                    height=max(w.box.y + w.box.height for w in chunk) - y,
                                )
                                self.anchors.setdefault(name, (box, chunk))

    def value(self, name: str, path: str | None = None) -> str | None:
        if name not in self.anchors:
            return None
        anchor, anchor_words = self.anchors[name]
        left = anchor.x - max(6, anchor.height * 2)
        right = min(
            [
                box.x - max(6, box.height * 2)
                for box, _ in self.anchors.values()
                if box.x > anchor.x + anchor.width and abs(box.y - anchor.y) < anchor.height * 8
            ]
            or [self.doc.width]
        )
        bottom = min(
            [
                box.y
                for other, (box, _) in self.anchors.items()
                if other != name
                and box.y > anchor.y + anchor.height
                and anchor.x - 10 <= box.x < right
            ]
            or [self.doc.height]
        )
        if name not in {"billing", "delivery"}:
            bottom = min(bottom, anchor.y + anchor.height * 6)
        words = [
            w
            for w in self.doc.words
            if w not in anchor_words
            and w.box.y >= anchor.y + anchor.height - 2
            and w.box.y + w.box.height <= bottom + 2
            and left <= w.box.x < right
        ]
        groups: dict[int, list[OCRWord]] = defaultdict(list)
        for word in sorted(words, key=lambda w: (w.box.y, w.box.x)):
            row = next(
                (y for y in groups if abs(y - word.box.y) < word.box.height * 0.6), word.box.y
            )
            groups[row].append(word)
        lines = [
            " ".join(w.text for w in sorted(group, key=lambda w: w.box.x))
            for group in groups.values()
        ]
        if not lines:
            return None
        selected = words if name in {"billing", "delivery"} else groups[next(iter(groups))]
        value = "\n".join(lines) if name in {"billing", "delivery"} else lines[0]
        if path:
            self.evidence.append(
                FieldEvidence(
                    field=path,
                    raw_text=value,
                    engine=self.doc.engine,
                    boxes=[w.box for w in selected],
                    confidence=min(w.confidence for w in selected),
                )
            )
        return value

    def address(self, name: str) -> Address:
        block = self.value(name, f"customer.{name}")
        if not block:
            return Address()
        lines = block.splitlines()
        postal = next((i for i, line in enumerate(lines) if re.match(r"^\d{4,10}\s+", line)), None)
        if postal is None or postal < 1 or postal + 1 >= len(lines):
            self.issues.append(f"Cannot independently segment {name} address")
            return Address()
        postcode, city = lines[postal].split(maxsplit=1)
        return Address(
            recipient=lines[0],
            additional_name=" ".join(lines[1 : postal - 1]) or None,
            street=lines[postal - 1],
            postcode=postcode,
            city=city,
            country=lines[postal + 1],
        )

    def parse(self) -> ExtractionResult:
        def numeric(name, path=None):
            raw = self.value(name, path or name)
            if raw is None:
                return None
            try:
                return number(raw)
            except (ValueError, InvalidOperation):
                self.issues.append(f"Unclear number: {name}")
                return None

        def day(name, path=None):
            raw = self.value(name, path or name)
            try:
                return date.fromisoformat(raw) if raw else None
            except ValueError:
                self.issues.append(f"Unclear ISO date: {name}")
                return None

        contact = self.value("contact", "customer.contact")
        names = contact.split() if contact else []
        if len(names) > 2:
            self.issues.append("Contact name requires explicit first/last-name review")
        customer = Customer(
            company=self.value("company", "customer.company"),
            first_name=names[0] if len(names) == 2 else None,
            last_name=names[-1] if names else None,
            alias=self.value("alias", "customer.alias"),
            email=self.value("email", "customer.email"),
            telephone=self.value("telephone", "customer.telephone"),
            source_customer_id=self.value("source_customer_id", "customer.source_customer_id"),
            billing=self.address("billing"),
            delivery=self.address("delivery"),
        )
        currency = self.value("currency", "currency")
        if currency and currency.replace(" ", "") in {"EUR", "USD", "GBP"}:
            currency = currency.replace(" ", "")
        order = OrderInput(
            order_date=day("order_date"),
            external_reference=self.value("external_reference", "external_reference"),
            currency=currency,
            customer=customer,
            payment=PaymentInput(
                method=self.value("method", "payment.method"),
                status=self.value("status", "payment.status"),
                payment_date=day("payment_date", "payment.payment_date"),
            ),
            totals=Totals(
                net=numeric("net", "totals.net"),
                vat=numeric("vat", "totals.vat"),
                gross=numeric("gross", "totals.gross"),
            ),
            document_discount_percent=numeric("document_discount_percent"),
            shipping_net=numeric("shipping_net"),
            shipping_vat_percent=numeric("shipping_vat_percent"),
        )
        start = self.anchors.get("items", (Box(x=0, y=self.doc.height, width=1, height=1), []))[0].y
        end = min(
            [
                self.anchors[n][0].y
                for n in ("net", "vat", "gross", "document_discount_percent", "shipping_net")
                if n in self.anchors
            ]
            or [self.doc.height]
        )
        num = r"(?:\d[\d.,]*%?)"
        pattern = re.compile(
            rf"^(?:\d+\s+)?(\S+)\s+(.+?)\s+({num})\s+(\S+)\s+({num})\s+({num})\s+({num})\s+({num})$"
        )
        for text, words in self.doc.lines():
            if not start < min(w.box.y for w in words) < end:
                continue
            match = pattern.match(normalize(text))
            if not match:
                continue
            sku, description, qty, unit, price, discount, vat, total = match.groups()
            try:
                item = ItemInput(
                    sku=sku,
                    description=description,
                    quantity=number(qty),
                    unit=unit,
                    unit_net=number(price),
                    discount_percent=number(discount),
                    vat_percent=number(vat),
                    printed_net=number(total),
                )
            except ValueError:
                self.issues.append(f"Unclear item row: {text}")
                continue
            row = len(order.items)
            order.items.append(item)
            for field in ItemInput.model_fields:
                self.evidence.append(
                    FieldEvidence(
                        field=f"items.{row}.{field}",
                        raw_text=text,
                        engine=self.doc.engine,
                        boxes=[w.box for w in words],
                        confidence=min(w.confidence for w in words),
                    )
                )
        if not order.items:
            self.issues.append("No complete independently parsed item rows")
        return ExtractionResult(order=order, evidence=self.evidence, issues=self.issues)


def compare_readings(ocr: ExtractionResult, vision: ExtractionResult) -> list[dict]:
    from .validation import flatten

    independent, candidate = flatten(ocr.order), flatten(vision.order)
    findings = []
    if len(ocr.order.items) != len(vision.order.items):
        findings.append(
            {
                "field": "items",
                "code": "ROW_COUNT_DISAGREEMENT",
                "ocr": len(ocr.order.items),
                "vision": len(vision.order.items),
            }
        )
    for path, wanted in candidate.items():
        seen = independent.get(path)
        if wanted is None and seen is None:
            continue
        if wanted is not None and seen is not None:
            try:
                equal = (
                    Decimal(wanted) == Decimal(seen)
                    if any(
                        part in path
                        for part in (
                            "quantity",
                            "unit_net",
                            "percent",
                            "printed_net",
                            "totals.",
                            "shipping_net",
                        )
                    )
                    else normalize(str(wanted)) == normalize(str(seen))
                )
            except InvalidOperation:
                equal = False
        else:
            equal = False
        if not equal:
            findings.append(
                {"field": path, "code": "EXTRACTION_DISAGREEMENT", "ocr": seen, "vision": wanted}
            )
    for issue in ocr.issues + vision.issues:
        findings.append({"field": "source", "code": "EXTRACTION_ISSUE", "message": issue})
    return findings
