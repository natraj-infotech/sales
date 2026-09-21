"""PDF export for procurement deal comparison."""

from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from business.procurement import format_money

BLUE = HexColor("#2b6fd6")
INK = HexColor("#0f172a")
LINE = HexColor("#e2e8f0")
MUTED = HexColor("#64748b")
_FONTS_READY = False
_FONT_NAME = "Helvetica"


def _register_fonts() -> str:
    global _FONTS_READY, _FONT_NAME
    if _FONTS_READY:
        return _FONT_NAME
    windir = os.environ.get("WINDIR", r"C:\Windows")
    pairs = [
        (
            Path(windir) / "Fonts" / "segoeui.ttf",
            Path(windir) / "Fonts" / "segoeuib.ttf",
        ),
        (
            Path(windir) / "Fonts" / "arial.ttf",
            Path(windir) / "Fonts" / "arialbd.ttf",
        ),
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ),
    ]
    for regular, bold in pairs:
        if regular.is_file() and bold.is_file():
            try:
                pdfmetrics.registerFont(TTFont("ProcSans", str(regular)))
                pdfmetrics.registerFont(TTFont("ProcSans-Bold", str(bold)))
                _FONT_NAME = "ProcSans"
                _FONTS_READY = True
                return _FONT_NAME
            except Exception:
                continue
    _FONT_NAME = "Helvetica"
    _FONTS_READY = True
    return _FONT_NAME


def build_compare_pdf(deal_no: str, lines, ranked_rows, round_name: str) -> bytes:
    font = _register_fonts()
    bold = f"{font}-Bold" if font != "Helvetica" else "Helvetica-Bold"
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=f"Compare Deals {deal_no}",
    )
    styles = {
        "title": ParagraphStyle(
            "proc_title",
            fontName=bold,
            fontSize=16,
            textColor=INK,
            spaceAfter=4,
        ),
        "meta": ParagraphStyle(
            "proc_meta",
            fontName=font,
            fontSize=10,
            textColor=MUTED,
            spaceAfter=10,
        ),
        "cell": ParagraphStyle(
            "proc_cell",
            fontName=font,
            fontSize=8,
            textColor=INK,
            leading=11,
        ),
        "cell_right": ParagraphStyle(
            "proc_cell_right",
            fontName=font,
            fontSize=8,
            textColor=INK,
            alignment=TA_RIGHT,
            leading=11,
        ),
        "head": ParagraphStyle(
            "proc_head",
            fontName=bold,
            fontSize=8,
            textColor=white,
            leading=11,
        ),
    }
    header = ["Rank", "Vendor Name"]
    header.extend([line.product.name for line in lines])
    header.append("Note")
    data = [[Paragraph(escape_text(col), styles["head"]) for col in header]]
    for row in ranked_rows:
        rank = str(row["rank"]) if row.get("rank") else "—"
        cells = [
            Paragraph(rank, styles["cell"]),
            Paragraph(escape_text(row["vendor"].name), styles["cell"]),
        ]
        for cell in row["product_prices"]:
            cells.append(Paragraph(escape_text(cell["display"]), styles["cell_right"]))
        cells.append(Paragraph(escape_text(row.get("note") or "—"), styles["cell"]))
        data.append(cells)

    col_count = len(header)
    usable = landscape(A4)[0] - 28 * mm
    widths = [18 * mm, 45 * mm]
    remaining = usable - sum(widths) - 40 * mm
    product_cols = max(col_count - 3, 1)
    product_w = remaining / product_cols
    widths.extend([product_w] * product_cols)
    widths.append(40 * mm)

    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), BLUE),
                ("TEXTCOLOR", (0, 0), (-1, 0), white),
                ("FONTNAME", (0, 0), (-1, 0), bold),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.4, LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, HexColor("#f8fafc")]),
            ]
        )
    )
    story = [
        Paragraph(f"Compare Result — Deal {escape_text(deal_no)}", styles["title"]),
        Paragraph(
            f"Round: {escape_text(round_name)} &nbsp;|&nbsp; Rank (Low-High)",
            styles["meta"],
        ),
        table,
        Spacer(1, 8),
    ]
    doc.build(story)
    return buffer.getvalue()


def escape_text(value) -> str:
    text = str(value or "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
