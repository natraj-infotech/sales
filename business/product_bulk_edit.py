"""Export and import products for bulk editing."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from accounts.models import CompanyProfile
from business.import_io import cell_str, detect_header_row, iter_spreadsheet_rows, norm_header
from business.models import Product, Vendor
from openpyxl import Workbook

EXPORT_HEADERS: tuple[str, ...] = (
    "Product ID",
    "Name",
    "Category",
    "Type",
    "Company",
    "Tax",
    "Depreciation",
    "HSN Code",
    "Company Number",
    "Company Email",
    "Components",
    "Notes",
    "Vendor",
    "Company Part Number",
    "Sales Price",
    "Description",
)

HEADER_ALIASES: dict[str, str] = {
    "product_id": "product_id",
    "id": "product_id",
    "name": "name",
    "product_name": "name",
    "category": "category",
    "type": "asset_type",
    "asset_type": "asset_type",
    "company": "company_name",
    "company_name": "company_name",
    "tax": "tax",
    "tax_percentage": "tax",
    "depreciation": "depreciation",
    "depreciation_percentage": "depreciation",
    "hsn_code": "hsn_code",
    "hsn": "hsn_code",
    "company_number": "company_phone",
    "company_phone": "company_phone",
    "company_email": "company_email",
    "components": "components",
    "notes": "notes",
    "note": "notes",
    "vendor": "vendor",
    "preferred_vendor": "vendor",
    "company_part_number": "sku",
    "sku": "sku",
    "sales_price": "unit_price",
    "unit_price": "unit_price",
    "description": "description",
}

HINT_WORDS = frozenset(
    {
        "product",
        "name",
        "category",
        "type",
        "company",
        "tax",
        "depreciation",
        "hsn",
        "vendor",
        "sku",
        "price",
        "description",
        "components",
        "notes",
    }
)


def build_product_export_bytes(company: CompanyProfile) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Products"
    ws.append(list(EXPORT_HEADERS))
    for product in company.products.select_related("vendor").all().order_by("name"):
        ws.append(
            [
                product.pk,
                product.name,
                product.category,
                product.asset_type,
                product.company_name,
                product.tax,
                product.depreciation,
                product.hsn_code,
                product.company_phone,
                product.company_email,
                product.components,
                product.notes,
                product.vendor.name if product.vendor_id else "",
                product.sku,
                product.unit_price,
                product.description,
            ]
        )
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_column_map(header_row: list[Any]) -> dict[str, int]:
    col_map: dict[str, int] = {}
    for idx, cell in enumerate(header_row):
        key = norm_header(cell)
        if not key:
            continue
        field = HEADER_ALIASES.get(key)
        if field and field not in col_map:
            col_map[field] = idx
    return col_map


def validate_column_mapping(col_map: dict[str, int]) -> list[str]:
    if "name" not in col_map and "product_id" not in col_map:
        return ["The file must include a Product ID or Name column."]
    return []


def _parse_product_id(raw: Any) -> int | None:
    text = cell_str(raw)
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _parse_email(raw: Any) -> tuple[str | None, str | None]:
    email = cell_str(raw)
    if not email:
        return "", None
    try:
        validate_email(email)
    except ValidationError:
        return None, f"Invalid company email: {email}"
    return email, None


def _parse_decimal(raw: Any) -> Decimal:
    text = cell_str(raw).replace(",", "")
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _resolve_vendor(
    company: CompanyProfile,
    raw: Any,
    vendors_by_lower: dict[str, Vendor],
) -> tuple[Vendor | None, str | None]:
    name = cell_str(raw)
    if not name:
        return None, None
    vendor = vendors_by_lower.get(name.lower())
    if vendor is None:
        return None, f"Vendor not found: {name}"
    return vendor, None


def _upsert_product(
    company: CompanyProfile,
    get,
    products_by_id: dict[int, Product],
    products_by_name: dict[str, Product],
    vendors_by_lower: dict[str, Vendor],
) -> tuple[Product | None, str | None, bool]:
    product_id = _parse_product_id(get("product_id"))
    name = cell_str(get("name"))
    category = cell_str(get("category"))[:128]
    asset_type = cell_str(get("asset_type"))[:128]
    company_name = cell_str(get("company_name"))[:255]
    tax = cell_str(get("tax"))[:64]
    depreciation = cell_str(get("depreciation"))[:128]
    hsn_code = cell_str(get("hsn_code"))[:32]
    company_phone = cell_str(get("company_phone"))[:64]
    company_email, email_err = _parse_email(get("company_email"))
    if email_err:
        return None, email_err, False
    components = cell_str(get("components"))
    notes = cell_str(get("notes"))
    sku = cell_str(get("sku"))[:64]
    unit_price = _parse_decimal(get("unit_price"))
    description = cell_str(get("description"))
    vendor, vendor_err = _resolve_vendor(company, get("vendor"), vendors_by_lower)
    if vendor_err:
        return None, vendor_err, False

    existing: Product | None = None
    if product_id is not None:
        existing = products_by_id.get(product_id)
        if existing is None:
            return None, f"Product ID {product_id} was not found for your company.", False

    if existing is None and name:
        existing = products_by_name.get(name.lower())

    fields = {
        "category": category,
        "asset_type": asset_type,
        "company_name": company_name,
        "tax": tax,
        "depreciation": depreciation,
        "hsn_code": hsn_code,
        "company_phone": company_phone,
        "company_email": company_email or "",
        "components": components,
        "notes": notes,
        "vendor": vendor,
        "sku": sku,
        "unit_price": unit_price,
        "description": description,
    }

    if existing is None:
        if not name:
            return None, "Name is required for new products.", False
        product = Product.objects.create(company=company, name=name, **fields)
        products_by_id[product.pk] = product
        products_by_name[name.lower()] = product
        return product, None, True

    if name:
        existing.name = name
    for attr, value in fields.items():
        setattr(existing, attr, value)
    existing.save()
    products_by_id[existing.pk] = existing
    products_by_name[existing.name.strip().lower()] = existing
    return existing, None, False


def _import_product_rows(
    company: CompanyProfile,
    rows_iter: Iterator[tuple[Any, ...] | list[str]],
    *,
    header_row_index: int = 0,
) -> tuple[int, int, list[tuple[int, str]]]:
    errors: list[tuple[int, str]] = []
    created = 0
    updated = 0
    skipped_blank = 0

    all_rows = list(rows_iter)
    if not all_rows:
        return 0, 0, [(0, "The file has no rows.")]

    hdr_i = min(max(header_row_index, 0), len(all_rows) - 1)
    header_row = list(all_rows[hdr_i])
    data_rows = all_rows[hdr_i + 1 :]
    col_map = _build_column_map(header_row)
    map_errors = validate_column_mapping(col_map)
    if map_errors:
        return 0, 0, [(0, map_errors[0])]

    if not data_rows:
        return 0, 0, [(0, "No data rows found below the header row.")]

    products_by_id = {p.pk: p for p in company.products.all()}
    products_by_name = {p.name.strip().lower(): p for p in company.products.all()}
    vendors_by_lower = {v.name.strip().lower(): v for v in company.vendors.all()}
    max_col_idx = max(col_map.values()) if col_map else 0
    header_width = max(len(header_row), max_col_idx + 1)

    row_num = hdr_i + 1
    for data_row in data_rows:
        row_num += 1
        if data_row is None:
            continue
        data_row = tuple(data_row)
        pad_to = max(header_width, max_col_idx + 1)
        if len(data_row) < pad_to:
            data_row = data_row + (None,) * (pad_to - len(data_row))

        def get(field: str) -> Any:
            idx = col_map.get(field)
            if idx is None or idx >= len(data_row):
                return None
            return data_row[idx]

        if not cell_str(get("name")) and _parse_product_id(get("product_id")) is None:
            skipped_blank += 1
            continue

        _product, err, was_created = _upsert_product(
            company, get, products_by_id, products_by_name, vendors_by_lower
        )
        if err:
            errors.append((row_num, err))
            continue
        if was_created:
            created += 1
        else:
            updated += 1

    if created == 0 and updated == 0 and not errors:
        if skipped_blank:
            errors.append(
                (
                    0,
                    f"All {skipped_blank} data row(s) look empty. "
                    "Include Product ID or Name on each row you want to update.",
                )
            )
        else:
            errors.append((0, "No product rows could be imported from this file."))

    return created, updated, errors


def import_products_bulk_from_file(
    company: CompanyProfile,
    file_bytes: bytes,
    filename: str = "",
) -> tuple[int, int, list[tuple[int, str]]]:
    header_row_index, _header = detect_header_row(
        file_bytes,
        filename,
        HEADER_ALIASES,
        HINT_WORDS,
    )
    rows = iter_spreadsheet_rows(file_bytes, filename)
    return _import_product_rows(
        company,
        rows,
        header_row_index=header_row_index,
    )
