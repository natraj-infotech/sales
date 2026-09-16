"""PDF download for the client renewal report."""

from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

from django.utils import timezone
from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from business.analytics import RENEWAL_REPORT_RANGES, renewal_report_charts

BLUE = HexColor("#2563eb")
BLUE_DEEP = HexColor("#1e40af")
SLATE = HexColor("#64748b")
INK = HexColor("#0f172a")
LINE = HexColor("#e2e8f0")
AMBER = HexColor("#d97706")
GREEN = HexColor("#059669")
RED = HexColor("#dc2626")
HEADER_FILL = HexColor("#1e5bb8")
PAGE_SIZE = landscape(A4)
PAGE_W, PAGE_H = PAGE_SIZE
_RANGE_SLUGS = {"30": "last_30_days", "90": "last_90_days", "year": "this_year"}
_REMAINING_SHORT = {
    "Expired": "Expired",
    "0–7 days": "0-7d",
    "8–15 days": "8-15d",
    "16–30 days": "16-30d",
    "31–60 days": "31-60d",
}
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
                pdfmetrics.registerFont(TTFont("ReportSans", str(regular)))
                pdfmetrics.registerFont(TTFont("ReportSans-Bold", str(bold)))
                _FONT_NAME = "ReportSans"
                _FONTS_READY = True
                return _FONT_NAME
            except Exception:
                continue
    _FONT_NAME = "Helvetica"
    _FONTS_READY = True
    return _FONT_NAME


def _range_label(range_key: str) -> str:
    for key, label in RENEWAL_REPORT_RANGES:
        if key == range_key:
            return label
    return "This year"


def _clip(text, limit=18) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _styles(font: str) -> dict[str, ParagraphStyle]:
    bold = f"{font}-Bold" if font != "Helvetica" else "Helvetica-Bold"
    return {
        "company": ParagraphStyle(
            "rrCompany",
            fontName=bold,
            fontSize=11,
            textColor=INK,
            leading=14,
        ),
        "meta": ParagraphStyle(
            "rrMeta",
            fontName=font,
            fontSize=8.5,
            textColor=SLATE,
            leading=12,
        ),
        "kpi_label": ParagraphStyle(
            "rrKpiLabel",
            fontName=bold,
            fontSize=6.5,
            textColor=SLATE,
            leading=9,
            alignment=TA_LEFT,
        ),
        "kpi_value": ParagraphStyle(
            "rrKpiValue",
            fontName=bold,
            fontSize=13,
            textColor=BLUE_DEEP,
            leading=16,
        ),
        "kpi_sub": ParagraphStyle(
            "rrKpiSub",
            fontName=font,
            fontSize=6.5,
            textColor=SLATE,
            leading=9,
        ),
        "legend": ParagraphStyle(
            "rrLegend",
            fontName=font,
            fontSize=7,
            textColor=INK,
            leading=9,
        ),
        "chart_title": ParagraphStyle(
            "rrChartTitle",
            fontName=bold,
            fontSize=9,
            textColor=INK,
            leading=12,
            spaceAfter=2,
        ),
        "section": ParagraphStyle(
            "rrSection",
            fontName=bold,
            fontSize=11,
            textColor=INK,
            leading=14,
            spaceBefore=8,
            spaceAfter=6,
        ),
        "empty": ParagraphStyle(
            "rrEmpty",
            fontName=font,
            fontSize=8,
            textColor=SLATE,
            leading=11,
        ),
        "th": ParagraphStyle(
            "rrTh",
            fontName=bold,
            fontSize=7,
            textColor=white,
            leading=9,
        ),
        "td": ParagraphStyle(
            "rrTd",
            fontName=font,
            fontSize=7,
            textColor=INK,
            leading=9,
        ),
    }


def _empty_chart(width, height, message="No data"):
    drawing = Drawing(width, height)
    drawing.add(
        String(
            width / 2,
            height / 2,
            message,
            fontSize=8,
            fillColor=SLATE,
            textAnchor="middle",
        )
    )
    return drawing


def _style_bar(chart, names):
    chart.categoryAxis.categoryNames = names
    chart.categoryAxis.labels.fontSize = 7
    chart.categoryAxis.labels.fillColor = SLATE
    chart.categoryAxis.labels.fontName = "Helvetica"
    chart.valueAxis.labels.fontSize = 7
    chart.valueAxis.labels.fillColor = SLATE
    chart.valueAxis.valueMin = 0
    chart.valueAxis.visibleGrid = 1
    chart.valueAxis.gridStrokeColor = LINE
    chart.barSpacing = 2.5
    chart.groupSpacing = 8
    chart.strokeColor = None
    chart.valueAxis.strokeColor = LINE
    chart.categoryAxis.strokeColor = LINE


def _reverse_series(names, data):
    return list(reversed(names)), [list(reversed(row)) for row in data]


def _bar_chart(labels, values, color, width, height, horizontal=False, label_chars=24):
    labels = list(labels or [])
    values = [float(v or 0) for v in (values or [])]
    if not labels:
        return _empty_chart(width, height)
    drawing = Drawing(width, height)
    names = [_clip(label, label_chars) for label in labels]
    data = [values]
    if horizontal:
        names, data = _reverse_series(names, data)
        label_w = min(140, max(88, width * 0.32))
        chart = HorizontalBarChart()
        chart.x = label_w
        chart.y = 18
        chart.height = height - 32
        chart.width = max(80, width - label_w - 18)
        chart.data = data
        _style_bar(chart, names)
        chart.categoryAxis.labels.boxAnchor = "e"
        chart.categoryAxis.labels.dx = -4
    else:
        chart = VerticalBarChart()
        chart.x = 30
        chart.y = 30
        chart.height = height - 42
        chart.width = width - 42
        chart.data = data
        _style_bar(chart, names)
        chart.categoryAxis.labels.angle = 0
        chart.categoryAxis.labels.boxAnchor = "n"
        chart.categoryAxis.labels.dy = -8
    chart.bars[0].fillColor = HexColor(color)
    chart.bars[0].strokeWidth = 0
    drawing.add(chart)
    return drawing


def _grouped_bar(labels, series, colors, width, height, horizontal=False, label_chars=24):
    labels = list(labels or [])
    if not labels:
        return _empty_chart(width, height)
    drawing = Drawing(width, height)
    names = [_clip(label, label_chars) for label in labels]
    data = []
    for _title, values in series:
        row = [float(v or 0) for v in (values or [])]
        if len(row) < len(names):
            row.extend([0] * (len(names) - len(row)))
        data.append(row[: len(names)])
    if horizontal:
        names, data = _reverse_series(names, data)
        label_w = min(140, max(90, width * 0.26))
        chart = HorizontalBarChart()
        chart.x = label_w
        chart.y = 18
        chart.height = height - 32
        chart.width = max(80, width - label_w - 18)
        chart.data = data
        _style_bar(chart, names)
        chart.categoryAxis.labels.boxAnchor = "e"
        chart.categoryAxis.labels.dx = -4
    else:
        chart = VerticalBarChart()
        chart.x = 30
        chart.y = 30
        chart.height = height - 42
        chart.width = width - 42
        chart.data = data
        _style_bar(chart, names)
        chart.categoryAxis.labels.angle = 0
        chart.categoryAxis.labels.boxAnchor = "n"
        chart.categoryAxis.labels.dy = -8
    for index, color in enumerate(colors):
        chart.bars[index].fillColor = HexColor(color)
        chart.bars[index].strokeWidth = 0
    drawing.add(chart)
    return drawing


def _doughnut_chart(values, colors, center, sub, width, height):
    values = [float(v or 0) for v in (values or [])]
    if not any(values):
        return _empty_chart(width, height, "No deals yet")
    drawing = Drawing(width, height)
    size = min(width - 8, height - 8)
    pie = Pie()
    pie.x = (width - size) / 2
    pie.y = (height - size) / 2
    pie.width = size
    pie.height = size
    pie.data = values
    pie.labels = [""] * len(values)
    pie.innerRadiusFraction = 0.62
    pie.slices.strokeWidth = 1.5
    pie.slices.strokeColor = white
    pie.startAngle = 90
    for index, color in enumerate(colors):
        pie.slices[index].fillColor = HexColor(color)
    drawing.add(pie)
    drawing.add(
        String(
            width / 2,
            height / 2 + 6,
            str(center),
            fontSize=12,
            fillColor=INK,
            textAnchor="middle",
            fontName="Helvetica-Bold",
        )
    )
    drawing.add(
        String(
            width / 2,
            height / 2 - 8,
            sub,
            fontSize=7,
            fillColor=SLATE,
            textAnchor="middle",
        )
    )
    return drawing


def _kpi_card(item, styles, value_color, width):
    value_style = ParagraphStyle(
        f"rrKpiValue-{item['label']}",
        parent=styles["kpi_value"],
        textColor=value_color,
    )
    inner = Table(
        [
            [Paragraph(item["label"].upper(), styles["kpi_label"])],
            [Paragraph(str(item["value"]), value_style)],
            [Paragraph(item["sub"], styles["kpi_sub"])],
        ],
        colWidths=[width],
    )
    inner.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (0, 0), 7),
                ("TOPPADDING", (0, 1), (0, 1), 1),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 7),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, -1), white),
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
            ]
        )
    )
    return inner


def _kpi_grid(items, styles, page_width):
    gap = 6
    card_w = (page_width - gap * 3) / 4
    cards = []
    colors = [AMBER, BLUE_DEEP, GREEN, RED, BLUE_DEEP, BLUE_DEEP, BLUE_DEEP, BLUE_DEEP]
    for item, color in zip(items, colors):
        cards.append(_kpi_card(item, styles, color, card_w))
    rows = [cards[0:4], cards[4:8]]
    table = Table(rows, colWidths=[card_w] * 4, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), gap),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), gap),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def _legend_row(pairs, styles, width):
    if not pairs:
        return Spacer(1, 1)
    count = max(len(pairs), 1)
    col_w = width / count
    cells = []
    for color, label in pairs:
        swatch = Table([[""]], colWidths=[8], rowHeights=[8])
        swatch.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), HexColor(color)),
                    ("BOX", (0, 0), (-1, -1), 0.4, HexColor(color)),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        item = Table(
            [[swatch, Paragraph(label, styles["legend"])]],
            colWidths=[12, max(40, col_w - 16)],
        )
        item.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (0, 0), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ]
            )
        )
        cells.append(item)
    table = Table([cells], colWidths=[col_w] * count)
    table.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    return table


def _legend_stack(pairs, styles, width):
    rows = []
    for color, label in pairs:
        swatch = Table([[""]], colWidths=[8], rowHeights=[8])
        swatch.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), HexColor(color)),
                    ("BOX", (0, 0), (-1, -1), 0.4, HexColor(color)),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        rows.append([swatch, Paragraph(label, styles["legend"])])
    table = Table(rows, colWidths=[14, max(40, width - 14)])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _chart_card(title, drawing, width, styles, legend_pairs=None):
    inner_w = width - 16
    rows = [[Paragraph(title, styles["chart_title"])]]
    if legend_pairs:
        rows.append([_legend_row(legend_pairs, styles, inner_w)])
    rows.append([drawing])
    block = Table(rows, colWidths=[inner_w])
    block.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), white),
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (0, 0), 8),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
                ("TOPPADDING", (0, 1), (-1, -2), 2),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return block


def _n_col_row(cells, page_width):
    count = len(cells)
    gap = 8
    width = (page_width - gap * (count - 1)) / count
    row = []
    col_widths = []
    for index, cell in enumerate(cells):
        if index:
            row.append("")
            col_widths.append(gap)
        row.append(cell)
        col_widths.append(width)
    table = Table([row], colWidths=col_widths, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def _data_table(headers, rows, styles, col_widths):
    header = [Paragraph(text, styles["th"]) for text in headers]
    data = [header]
    if rows:
        for row in rows:
            data.append([Paragraph(str(value or "—"), styles["td"]) for value in row])
    else:
        empty = Paragraph("No rows for this section.", styles["empty"])
        data.append([empty] + [""] * (len(headers) - 1))
    table = Table(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_FILL),
        ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ("FONTNAME", (0, 0), (-1, 0), styles["th"].fontName),
        ("BACKGROUND", (0, 1), (-1, -1), white),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for i in range(1, len(data)):
        if i % 2 == 0:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), HexColor("#f8fafc")))
    table.setStyle(TableStyle(style_cmds))
    return table


def build_renewal_report_bytes(company, *, range_key="year"):
    range_key = (range_key or "year").strip().lower()
    if range_key not in {"30", "90", "year"}:
        range_key = "year"
    today = timezone.localdate()
    report = renewal_report_charts(company, range_key=range_key, today=today)
    charts = report["charts"]
    range_label = report.get("range_label") or _range_label(range_key)
    generated = timezone.localtime().strftime("%d %b %Y, %H:%M")
    company_name = company.company_name or "Company"
    font = _register_fonts()
    styles = _styles(font)
    page_width = PAGE_W - 36 * 2
    col3 = (page_width - 16) / 3
    col2 = (page_width - 8) / 2
    overview_h = 128
    full_h = 178
    half_h = 168
    remain_labels = [
        _REMAINING_SHORT.get(label, label) for label in charts["remaining_labels"]
    ]
    deal_colors = ["#1e5bb8", "#60a5fa", "#94a3b8"]
    deal_legend = [
        (color, f"{label} {int(value)}")
        for label, value, color in zip(
            charts["deal_labels"], charts["deal_values"], deal_colors
        )
    ]
    deal_w = col3 - 16
    deal_inner = Table(
        [
            [
                _doughnut_chart(
                    charts["deal_values"],
                    deal_colors,
                    charts["deal_total"],
                    "deals",
                    deal_w * 0.52,
                    overview_h,
                ),
                _legend_stack(deal_legend, styles, deal_w * 0.44),
            ]
        ],
        colWidths=[deal_w * 0.55, deal_w * 0.45],
    )
    deal_inner.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    kpis = [
        {
            "label": "Pending renewals",
            "value": report["pending_renewals"],
            "sub": "Active, next 60 days, no Win/Lost",
        },
        {
            "label": "Silent expired",
            "value": report["silent_expired"],
            "sub": "End date passed, still no decision",
        },
        {
            "label": "Re-subscription rate",
            "value": f"{report['resub_rate']}%",
            "sub": (
                f"{report['win_count']} win / {report['lost_count']} lost · "
                f"Up {report['qty_up']} · Same {report['qty_same']} · Down {report['qty_down']}"
            ),
        },
        {
            "label": "Did not renew",
            "value": report["did_not_renew"],
            "sub": f"{report['lost_count']} lost + {report['silent_expired']} silent",
        },
        {
            "label": "New products",
            "value": report["new_product_qty"],
            "sub": f"{report['new_product_clients']} companies requested add-ons",
        },
        {
            "label": "Billing at risk",
            "value": report["billing_at_risk"],
            "sub": "Pending renewals, current amount",
        },
        {
            "label": "Avg send delay",
            "value": report["avg_send_delay"],
            "sub": "Eligible window start to first email",
        },
        {
            "label": "Avg email to deal",
            "value": report["avg_deal_delay"],
            "sub": "Reminder sent to Win/Lost set",
        },
    ]

    story = [
        Paragraph(company_name, styles["company"]),
        Paragraph(
            f"{range_label} for emails, deals, and new products. "
            "Remaining renewals are the live snapshot. "
            f"Generated {generated}.",
            styles["meta"],
        ),
        Spacer(1, 8),
        _kpi_grid(kpis, styles, page_width),
        Spacer(1, 6),
        _n_col_row(
            [
                _chart_card(
                    "Renewal funnel",
                    _bar_chart(
                        charts["funnel_labels"],
                        charts["funnel_values"],
                        "#2563eb",
                        col3 - 16,
                        overview_h,
                    ),
                    col3,
                    styles,
                ),
                _chart_card("Deal outcome", deal_inner, col3, styles),
                _chart_card(
                    "Remaining renewals",
                    _bar_chart(
                        remain_labels,
                        charts["remaining_values"],
                        "#f59e0b",
                        col3 - 16,
                        overview_h,
                    ),
                    col3,
                    styles,
                ),
            ],
            page_width,
        ),
        Spacer(1, 8),
        _chart_card(
            "Time by company",
            _grouped_bar(
                charts["timing_labels"],
                [
                    ("Send delay (days)", charts["timing_send_days"]),
                    ("Email to deal (days)", charts["timing_deal_days"]),
                ],
                ["#93c5fd", "#1e5bb8"],
                page_width - 16,
                full_h,
                horizontal=True,
                label_chars=28,
            ),
            page_width,
            styles,
            legend_pairs=[
                ("#93c5fd", "Send delay (days)"),
                ("#1e5bb8", "Email to deal (days)"),
            ],
        ),
        Spacer(1, 8),
        _n_col_row(
            [
                _chart_card(
                    "New products requested",
                    _bar_chart(
                        charts["new_product_labels"],
                        charts["new_product_values"],
                        "#0ea5e9",
                        col2 - 16,
                        half_h,
                        horizontal=True,
                        label_chars=28,
                    ),
                    col2,
                    styles,
                ),
                _chart_card(
                    "Monthly trend",
                    _grouped_bar(
                        charts["trend_labels"],
                        [
                            ("Sent", charts["trend_sent"]),
                            ("Submitted", charts["trend_submitted"]),
                            ("Win", charts["trend_win"]),
                            ("Lost", charts["trend_lost"]),
                        ],
                        ["#93c5fd", "#3b82f6", "#059669", "#f43f5e"],
                        col2 - 16,
                        half_h,
                    ),
                    col2,
                    styles,
                    legend_pairs=[
                        ("#93c5fd", "Sent"),
                        ("#3b82f6", "Submitted"),
                        ("#059669", "Win"),
                        ("#f43f5e", "Lost"),
                    ],
                ),
            ],
            page_width,
        ),
        Spacer(1, 10),
        Paragraph("Remaining renewals", styles["section"]),
        _data_table(
            [
                "Client",
                "Product",
                "End date",
                "Days left",
                "Last email",
                "Opened",
                "Submitted",
                "Deal",
                "Amount",
            ],
            [
                [
                    row["client"],
                    row["product"],
                    row["end_date"],
                    row["days_left"],
                    row["last_email"] or "—",
                    row["opened"],
                    row["submitted"],
                    row["deal"],
                    row["amount"],
                ]
                for row in report["remaining_rows"]
            ],
            styles,
            [102, 118, 78, 82, 102, 55, 70, 58, 105],
        ),
        Spacer(1, 12),
        Paragraph("Timing by company", styles["section"]),
        _data_table(
            [
                "Client",
                "Product",
                "Email sent",
                "Opened",
                "Submitted",
                "Deal set",
                "Send delay",
                "Email to deal",
            ],
            [
                [
                    row["client"],
                    row["product"],
                    row["email_sent"] or "—",
                    row["opened"],
                    row["submitted"] or "—",
                    row["deal_set"] or "—",
                    row["send_delay"],
                    row["deal_delay"],
                ]
                for row in report["timing_rows"]
            ],
            styles,
            [115, 120, 95, 50, 100, 100, 80, 110],
        ),
        Spacer(1, 12),
        Paragraph("New products on renewal", styles["section"]),
        _data_table(
            ["Client", "Existing product", "New product asked", "Qty", "Date"],
            [
                [row["client"], row["existing"], row["product"], row["quantity"], row["date"]]
                for row in report["new_product_rows"]
            ],
            styles,
            [160, 195, 195, 50, 170],
        ),
        Spacer(1, 12),
        Paragraph("Did not renew", styles["section"]),
        _data_table(
            ["Client", "Product", "Expired on", "Last reminder", "Form filled", "Status"],
            [
                [
                    row["client"],
                    row["product"],
                    row["end_date"],
                    row["last_email"] or "—",
                    row["form_filled"],
                    row["reason"],
                ]
                for row in report["did_not_renew_rows"]
            ],
            styles,
            [140, 145, 100, 125, 80, 180],
        ),
    ]

    buf = BytesIO()
    bold_font = f"{font}-Bold" if font != "Helvetica" else "Helvetica-Bold"

    def _draw_page(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(BLUE)
        canvas.rect(0, PAGE_H - 28, PAGE_W, 28, fill=1, stroke=0)
        canvas.setFillColor(white)
        canvas.setFont(bold_font, 11)
        canvas.drawString(36, PAGE_H - 18, "Client renewal report")
        canvas.setFont(font, 8)
        canvas.drawRightString(PAGE_W - 36, PAGE_H - 18, f"Page {doc.page}")
        canvas.setFillColor(HexColor("#94a3b8"))
        canvas.setFont(font, 8)
        canvas.drawString(36, 14, company_name)
        canvas.drawRightString(PAGE_W - 36, 14, generated)
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buf,
        pagesize=PAGE_SIZE,
        leftMargin=36,
        rightMargin=36,
        topMargin=40,
        bottomMargin=28,
        title="Client renewal report",
        author=company_name,
    )
    doc.build(story, onFirstPage=_draw_page, onLaterPages=_draw_page)
    slug = _RANGE_SLUGS.get(range_key, "this_year")
    return buf.getvalue(), f"client_renewal_report_{slug}.pdf"
