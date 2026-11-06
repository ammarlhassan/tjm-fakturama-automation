"""Build the design PDF from docs/design.md, its only source of prose. Limit: 1-4 pages."""

import re
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parents[1]
FLOW = [
    ["IMAGE", "INDEPENDENT READINGS", "ACCEPTANCE"],
    ["CLI / localhost API", "Local OCR + hosted vision", "Evidence + Decimal checks"],
    ["DURABLE JOURNAL", "SINGLE DESKTOP WORKER", "SAVED-RECORD PROOF"],
    ["Intent / observation / review", "Fakturama UI only", "Receipt + portable report"],
]


def main():
    markdown = (ROOT / "docs/design.md").read_text(encoding="utf-8")
    title = markdown.split("\n", 1)[0].lstrip("# ").strip()
    sections = []
    for block in markdown.split("\n## ")[1:]:
        heading, body = block.split("\n", 1)
        sections.append((heading, re.sub(r"```.*?```", "", body, flags=re.S).strip()))
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="Body",
            fontName="Helvetica",
            fontSize=9.4,
            leading=12.6,
            textColor=colors.HexColor("#23364a"),
            spaceAfter=6,
            alignment=TA_LEFT,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Item",
            parent=styles["Body"],
            leftIndent=11,
            bulletIndent=1,
            spaceAfter=3,
        )
    )
    styles["Title"].textColor = colors.HexColor("#14344d")
    styles["Title"].fontSize = 17
    styles["Title"].spaceAfter = 6
    styles["Heading2"].textColor = colors.HexColor("#21635a")
    styles["Heading2"].fontSize = 11.5
    styles["Heading2"].spaceBefore = 7
    styles["Heading2"].spaceAfter = 3
    styles["Heading2"].keepWithNext = 1

    def inline(text):
        text = escape(text.replace("\n", " "))
        text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
        return re.sub(r"`([^`]+)`", r"\1", text)

    story = [Paragraph(escape(title), styles["Title"])]
    for index, (heading, body) in enumerate(sections):
        story.append(Paragraph(escape(heading), styles["Heading2"]))
        for block in body.split("\n\n"):
            items = [line[2:] for line in block.splitlines() if line.startswith("- ")]
            if items and len(items) == len(block.splitlines()):
                for item in items:
                    story.append(Paragraph(inline(item), styles["Item"], bulletText="•"))
            else:
                story.append(Paragraph(inline(block), styles["Body"]))
        if index == 1:
            flow = Table(FLOW, colWidths=[160, 170, 160])
            flow.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#14344d")),
                        ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#21635a")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("TEXTCOLOR", (0, 2), (-1, 2), colors.white),
                        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#eef3f7")),
                        ("BACKGROUND", (0, 3), (-1, 3), colors.HexColor("#eef3f7")),
                        ("FONTSIZE", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ]
                )
            )
            story.extend([flow, Spacer(1, 8)])

    destination = ROOT / "docs/design.pdf"
    document = SimpleDocTemplate(
        str(destination),
        pagesize=A4,
        rightMargin=48,
        leftMargin=48,
        topMargin=40,
        bottomMargin=40,
        title="TJM Image-to-Cash: Design",
        author="Ammar",
    )

    def footer(canvas, doc):
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#53637a"))
        canvas.drawString(48, 24, "TJM Image-to-Cash design. Built from docs/design.md.")
        canvas.drawRightString(A4[0] - 48, 24, f"Page {doc.page}")

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    import pypdfium2

    with pypdfium2.PdfDocument(destination) as pdf:
        pages = len(pdf)
    if not 1 <= pages <= 4:
        raise RuntimeError(f"The design must be 1-4 pages; generated {pages}")
    print(destination, f"{pages} pages")


if __name__ == "__main__":
    main()
