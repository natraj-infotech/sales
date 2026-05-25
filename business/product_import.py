"""Parse CSV and Excel files for bulk Product import."""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from typing import Any, Callable

from accounts.models import CompanyProfile
from business.import_io import load_workbook_bytes, read_header_labels, read_raw_header_row
from business.models import Product, Vendor

IMPORT_HEADERS: tuple[str, ...] = (
    "Item ID",
    "Item Name",
    "HSN/SAC",
    "Is Taxable",
    "GST Rate (%)",
    "Cess Rate (%)",
    "Unit",
    "Account Code",
    "Type",
    "Description",
    "Purchase Price",
    "Sales Price",
    "Is Active",
    "Is Inventory",
    "Opening Stock",
    "Opening Stock Value",
    "Preferred Vendor",
    "Reorder Level",
    "Category",
    "Brand",
    "Manufacturer",
    "Model Number",
    "SKU",
    "UPC",
    "EAN",
    "MPN",
    "ISBN",
    "Color",
    "Size",
    "Material",
    "Weight",
    "Length",
    "Width",
    "Height",
    "Dimension Unit",
    "Weight Unit",
)

IMPORT_SAMPLE_ROW: tuple[Any, ...] = (
    "SKU-001",
    "Example product",
    "",
    "Yes",
    "18",
    "",
    "Nos",
    "",
    "Software",
    "Optional short description",
    "0",
    "999",
    "Yes",
    "No",
    "",
    "",
    "",
    "",
    "Subscription",
    "",
    "",
    "",
    "SKU-001",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
)

HEADER_ALIASES: dict[str, str] = {
    "item_id": "item_id",
    "item_name": "name",
    "name": "name",
    "product_name": "name",
    "hsn_sac": "hsn_code",
    "hsn_code": "hsn_code",
    "is_taxable": "is_taxable",
    "gst_rate": "tax",
    "gst_rate_%": "tax",
    "cess_rate": "cess_rate",
    "cess_rate_%": "cess_rate",
    "unit": "unit",
    "account_code": "account_code",
    "type": "asset_type",
    "asset_type": "asset_type",
    "description": "description",
    "purchase_price": "purchase_price",
    "sales_price": "unit_price",
    "unit_price": "unit_price",
    "price": "unit_price",
    "is_active": "is_active",
    "is_inventory": "is_inventory",
    "opening_stock": "opening_stock",
    "opening_stock_value": "opening_stock_value",
    "preferred_vendor": "vendor",
    "vendor": "vendor",
    "partner": "vendor",
    "reorder_level": "reorder_level",
    "category": "category",
    "brand": "brand",
    "manufacturer": "manufacturer",
    "model_number": "model_number",
    "sku": "sku",
    "upc": "upc",
    "ean": "ean",
    "mpn": "mpn",
    "isbn": "isbn",
    "color": "color",
    "size": "size",
    "material": "material",
    "weight": "weight",
    "length": "length",
    "width": "width",
    "height": "height",
    "dimension_unit": "dimension_unit",
    "weight_unit": "weight_unit",
    "company_part_number": "sku",
}

NOTE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Item ID", "item_id"),
    ("Is taxable", "is_taxable"),
    ("Cess rate (%)", "cess_rate"),
    ("Unit", "unit"),
    ("Account code", "account_code"),
    ("Purchase price", "purchase_price"),
    ("Is active", "is_active"),
    ("Is inventory", "is_inventory"),
    ("Opening stock", "opening_stock"),
    ("Opening stock value", "opening_stock_value"),
    ("Reorder level", "reorder_level"),
    ("Brand", "brand"),
    ("Manufacturer", "manufacturer"),
    ("Model number", "model_number"),
    ("UPC", "upc"),
    ("EAN", "ean"),
    ("MPN", "mpn"),
    ("ISBN", "isbn"),
    ("Color", "color"),
    ("Size", "size"),
    ("Material", "material"),
    ("Weight", "weight"),
    ("Length", "length"),
    ("Width", "width"),
    ("Height", "height"),
    ("Dimension unit", "dimension_unit"),
    ("Weight unit", "weight_unit"),
)

NOTE_SKIP_FIELDS = frozenset(
    {
        "name",
        "sku",
        "item_id",
        "hsn_code",
        "tax",
        "asset_type",
        "description",
        "unit_price",
        "category",
        "vendor",
    }
)

MAPPING_UI_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("name", "Item name", True),
    ("sku", "SKU", False),
    ("item_id", "Item ID", False),
    ("hsn_code", "HSN/SAC", False),
    ("category", "Category", False),
    ("asset_type", "Type", False),
    ("description", "Description", False),
    ("unit_price", "Sales price", False),
    ("tax", "GST rate (%)", False),
    ("vendor", "Preferred vendor", False),
)

ALLOWED_FIELDS = frozenset(HEADER_ALIASES.values()) | frozenset(
    f for _, f in NOTE_FIELDS
)


def _norm_header(val: Any) -> str:
    if val is None:
        return ""
    s = str(val).strip().lower()
    s = re.sub(r"[\s/()%]+", "_", s).strip("_")
    return s


def _cell_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return str(val).strip()


def _parse_decimal(val: Any) -> Decimal:
    if val is None or val == "":
        return Decimal("0")
    if isinstance(val, (int, float)):
        return Decimal(str(val))
    s = _cell_str(val).replace(",", "")
    if not s:
        return Decimal("0")
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal("0")


def _build_column_map(header_row: list[Any]) -> dict[str, int]:
    col_by_field: dict[str, int] = {}
    for idx, cell in enumerate(header_row):
        key = _norm_header(cell)
        if not key:
            continue
        field = HEADER_ALIASES.get(key, key if key in ALLOWED_FIELDS else None)
        if field and field not in col_by_field:
            col_by_field[field] = idx
    return col_by_field


def validate_column_mapping(col_map: dict[str, int]) -> list[str]:
    if "name" not in col_map:
        return ["Map a column to Item name."]
    return []


def column_mapping_from_post(
    post: Any,
    suggested: dict[str, int] | None = None,
) -> dict[str, int]:
    col_map: dict[str, int] = dict(suggested or {})
    for key, _, _ in MAPPING_UI_FIELDS:
        field_name = f"map_{key}"
        if field_name not in post:
            continue
        raw = (post.get(field_name) or "").strip()
        if not raw:
            col_map.pop(key, None)
            continue
        try:
            idx = int(raw)
        except ValueError:
            continue
        if idx >= 0:
            col_map[key] = idx
    return col_map


def auto_column_mapping(header_row: list[Any]) -> dict[str, int]:
    return _build_column_map(header_row)


def build_mapping_form_rows(
    file_headers: list[str],
    suggested: dict[str, int],
) -> list[dict[str, Any]]:
    choices = [
        (idx, f"Column {idx + 1}: {label}") for idx, label in enumerate(file_headers)
    ]
    return [
        {
            "key": key,
            "label": label,
            "required": required,
            "selected": suggested.get(key),
            "choices": choices,
        }
        for key, label, required in MAPPING_UI_FIELDS
    ]


def _read_header_row_raw(file_bytes: bytes, filename: str = "") -> list[Any]:
    return read_raw_header_row(file_bytes, filename)


def read_import_headers(file_bytes: bytes, filename: str = "") -> list[str]:
    return read_header_labels(file_bytes, filename)


def import_needs_column_mapping(file_bytes: bytes, filename: str = "") -> bool:
    raw_headers = _read_header_row_raw(file_bytes, filename)
    if not raw_headers:
        return True
    return bool(validate_column_mapping(auto_column_mapping(raw_headers)))


def _decode_csv_text(file_bytes: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("utf-8", errors="replace")


def _file_row_iterator(
    file_bytes: bytes, filename: str
) -> Iterator[tuple[Any, ...] | list[str]]:
    lower = (filename or "").lower()
    if lower.endswith(".csv"):
        return _iter_csv_rows(file_bytes)
    return _iter_xlsx_rows(file_bytes)


def _iter_xlsx_rows(file_bytes: bytes) -> Iterator[tuple[Any, ...]]:
    wb = load_workbook_bytes(file_bytes, read_only=True)
    try:
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            if row is not None:
                yield row
    finally:
        wb.close()


def _iter_csv_rows(file_bytes: bytes) -> Iterator[list[str]]:
    text = _decode_csv_text(file_bytes)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(StringIO(text), dialect)
    for row in reader:
        if row:
            yield row


def _get_or_create_vendor(
    company: CompanyProfile,
    vendor_name: str,
    vendors_by_lower: dict[str, Vendor],
) -> Vendor:
    key = vendor_name.strip().lower()
    existing = vendors_by_lower.get(key)
    if existing:
        return existing
    vendor = Vendor.objects.create(
        company=company,
        name=vendor_name.strip()[:255],
    )
    vendors_by_lower[key] = vendor
    return vendor


def _build_notes(get: Callable[[str], Any]) -> str:
    lines: list[str] = []
    for label, field in NOTE_FIELDS:
        if field in NOTE_SKIP_FIELDS:
            continue
        val = _cell_str(get(field))
        if val:
            lines.append(f"{label}: {val}")
    return "\n".join(lines)


def _upsert_product(
    company: CompanyProfile,
    get: Callable[[str], Any],
    products_by_name: dict[str, Product],
    products_by_sku: dict[str, Product],
    vendors_by_lower: dict[str, Vendor],
) -> tuple[Product | None, str | None, bool]:
    """Returns (product, error_message, was_created)."""
    name = _cell_str(get("name"))
    sku = _cell_str(get("sku")) or _cell_str(get("item_id"))
    if not name:
        name = sku
    if not name:
        return None, "Item Name is required.", False
    item_id = _cell_str(get("item_id"))
    if item_id and item_id not in (sku, name):
        pass  # captured in notes via item_id field

    existing = products_by_name.get(name.lower())
    if not existing and sku:
        existing = products_by_sku.get(sku.lower())

    vendor = None
    vendor_name = _cell_str(get("vendor"))
    if vendor_name:
        vendor = _get_or_create_vendor(company, vendor_name, vendors_by_lower)

    tax_val = _cell_str(get("tax"))
    if tax_val and not tax_val.endswith("%"):
        tax_val = tax_val

    notes = _build_notes(get)
    if item_id and item_id != sku:
        extra = f"Item ID: {item_id}"
        notes = f"{notes}\n{extra}".strip() if notes else extra

    fields = {
        "name": name[:255],
        "sku": sku[:64],
        "hsn_code": _cell_str(get("hsn_code"))[:32],
        "category": _cell_str(get("category"))[:128],
        "asset_type": _cell_str(get("asset_type"))[:128] or "Software",
        "description": _cell_str(get("description")),
        "unit_price": _parse_decimal(get("unit_price")),
        "tax": tax_val[:64],
        "vendor": vendor,
        "notes": notes,
    }

    if existing:
        for attr, value in fields.items():
            setattr(existing, attr, value)
        existing.save()
        products_by_name[name.lower()] = existing
        if existing.sku:
            products_by_sku[existing.sku.lower()] = existing
        return existing, None, False

    product = Product.objects.create(company=company, **fields)
    products_by_name[name.lower()] = product
    if product.sku:
        products_by_sku[product.sku.lower()] = product
    return product, None, True


def _import_product_rows(
    company: CompanyProfile,
    rows_iter: Iterator[tuple[Any, ...] | list[str]],
    column_mapping: dict[str, int] | None = None,
) -> tuple[int, int, list[tuple[int, str]]]:
    """Returns (created_count, updated_count, errors)."""
    errors: list[tuple[int, str]] = []
    created = 0
    updated = 0

    try:
        header_row = list(next(rows_iter))
    except StopIteration:
        return 0, 0, [(0, "The file has no rows.")]

    if column_mapping is not None:
        col_map = column_mapping
    else:
        col_map = _build_column_map(header_row)
    map_errors = validate_column_mapping(col_map)
    if map_errors:
        return 0, 0, [(0, map_errors[0])]

    products_by_name = {p.name.strip().lower(): p for p in company.products.all()}
    products_by_sku = {
        p.sku.strip().lower(): p
        for p in company.products.all()
        if (p.sku or "").strip()
    }
    vendors_by_lower = {v.name.strip().lower(): v for v in company.vendors.all()}
    max_col_idx = max(col_map.values()) if col_map else 0

    row_num = 1
    for data_row in rows_iter:
        row_num += 1
        if data_row is None:
            continue
        data_row = tuple(data_row)
        if len(data_row) <= max_col_idx:
            data_row = data_row + (None,) * (max_col_idx + 1 - len(data_row))

        def get(field: str) -> Any:
            idx = col_map.get(field)
            if idx is None or idx >= len(data_row):
                return None
            return data_row[idx]

        if not _cell_str(get("name")) and not _cell_str(get("sku")):
            continue

        product, err, was_created = _upsert_product(
            company, get, products_by_name, products_by_sku, vendors_by_lower
        )
        if err:
            errors.append((row_num, err))
            continue
        if was_created:
            created += 1
        else:
            updated += 1

    return created, updated, errors


def import_products_from_file(
    company: CompanyProfile,
    file_bytes: bytes,
    filename: str = "",
    column_mapping: dict[str, int] | None = None,
) -> tuple[int, int, list[tuple[int, str]]]:
    if not file_bytes:
        return 0, 0, [(0, "Empty file.")]

    lower = (filename or "").lower()
    if not (lower.endswith(".csv") or lower.endswith(".xlsx")):
        return 0, 0, [(0, "Unsupported file type. Upload a .xlsx or .csv file.")]

    try:
        rows_iter = _file_row_iterator(file_bytes, filename)
    except Exception as exc:  # noqa: BLE001
        kind = "CSV" if lower.endswith(".csv") else "Excel"
        return 0, 0, [(0, f"Could not read {kind} file: {exc}")]

    return _import_product_rows(company, rows_iter, column_mapping=column_mapping)
