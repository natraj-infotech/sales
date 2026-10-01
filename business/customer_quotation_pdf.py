"""PDF for customer-facing WhatsApp quotations."""

from __future__ import annotations

from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

from business.catalog import CustomerQuotation


def build_customer_quotation_pdf(quotation: CustomerQuotation) -> bytes | None:
    lines = list(quotation.lines.all())
    if not lines:
        return None

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=20 * mm, leftMargin=20 * mm)
    styles = getSampleStyleSheet()
    story = []

    company = quotation.company
    title = company.company_name if company else "Quotation"
    story.append(Paragraph(f"<b>{title}</b>", styles["Title"]))
    if quotation.customer_company_name:
        story.append(Paragraph(f"Customer: {quotation.customer_company_name}", styles["Normal"]))
    story.append(Paragraph(f"Quote #{quotation.pk}", styles["Normal"]))
    story.append(Spacer(1, 8 * mm))

    data = [["Item", "Qty", "Unit price", "Line total"]]
    for line in lines:
        unit = "On request"
        total = "—"
        if line.unit_price is not None and line.unit_price > 0:
            unit = f"₹{line.unit_price:,.2f}"
            if line.line_total is not None:
                total = f"₹{line.line_total:,.2f}"
        path = " / ".join(
            p
            for p in (
                line.master_category,
                line.category_name,
                line.subcategory_name,
            )
            if p
        )
        item = line.display_name
        if path:
            item = f"{line.display_name}<br/><font size='8'>{path}</font>"
        data.append([Paragraph(item, styles["Normal"]), str(line.quantity), unit, total])

    if quotation.subtotal > 0 and not quotation.has_unpriced_lines:
        data.append(["", "", "Total", f"₹{quotation.subtotal:,.2f}"])
    elif quotation.subtotal > 0 and quotation.has_unpriced_lines:
        data.append(["", "", "Subtotal (priced items)", f"₹{quotation.subtotal:,.2f}"])

    table = Table(data, colWidths=[90 * mm, 15 * mm, 35 * mm, 35 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2b6fd6")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
            ]
        )
    )
    story.append(table)
    if quotation.has_unpriced_lines:
        story.append(Spacer(1, 6 * mm))
        story.append(
            Paragraph(
                "<i>Items marked “On request” — our team will share best prices shortly.</i>",
                styles["Normal"],
            )
        )

    doc.build(story)
    return buffer.getvalue()
