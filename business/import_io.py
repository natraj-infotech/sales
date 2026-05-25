"""Shared helpers for reading spreadsheet header rows."""

from __future__ import annotations

import csv
import warnings
from io import BytesIO, StringIO
from typing import Any

from openpyxl import load_workbook


def load_workbook_bytes(file_bytes: bytes, *, read_only: bool = False):
    """Load xlsx bytes; suppress harmless openpyxl style warnings."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Workbook contains no default style*",
            category=UserWarning,
        )
        return load_workbook(
            BytesIO(file_bytes),
            read_only=read_only,
            data_only=True,
        )


def _decode_csv_text(file_bytes: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("utf-8", errors="replace")


def _read_csv_header_row(file_bytes: bytes) -> list[Any]:
    text = _decode_csv_text(file_bytes)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(StringIO(text), dialect)
    rows: list[list[str]] = []
    for i, row in enumerate(reader):
        if row:
            rows.append(row)
        if i >= 15:
            break
    if not rows:
        return []
    width = max(len(r) for r in rows)
    header = list(rows[0])
    if len(header) < width:
        header.extend([""] * (width - len(header)))
    return header


def _read_xlsx_header_row(file_bytes: bytes) -> list[Any]:
    """Read every column in row 1 (including empty trailing headers)."""
    wb = load_workbook_bytes(file_bytes, read_only=False)
    try:
        ws = wb.active
        if not ws.max_row or not ws.max_column:
            return []
        return [
            ws.cell(row=1, column=col).value for col in range(1, ws.max_column + 1)
        ]
    finally:
        wb.close()


def read_raw_header_row(file_bytes: bytes, filename: str = "") -> list[Any]:
    lower = (filename or "").lower()
    if lower.endswith(".csv"):
        return _read_csv_header_row(file_bytes)
    if lower.endswith(".xlsx"):
        return _read_xlsx_header_row(file_bytes)
    return []


def cell_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return str(val).strip()


def read_header_labels(file_bytes: bytes, filename: str = "") -> list[str]:
    """Human-readable label per column index for mapping dropdowns."""
    return [
        cell_str(cell) if cell_str(cell) else f"Column {idx + 1}"
        for idx, cell in enumerate(read_raw_header_row(file_bytes, filename))
    ]
