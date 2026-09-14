"""Parse CSV and Excel files for bulk ClientSubscription import."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from accounts.models import CompanyProfile
from business.import_io import (
    detect_header_row,
    iter_spreadsheet_rows,
    norm_header as _norm_header_io,
)
from business.models import Client, ClientSubscription, Product, Vendor

logger = logging.getLogger(__name__)

PRESET_VENDOR_CHOICES: tuple[tuple[str, str], ...] = (
    ("redington", "Redington"),
    ("tata", "Tata"),
    ("other", "Other"),
)

# Downloadable template and mapping labels.
IMPORT_HEADERS: tuple[str, ...] = (
    "Subscription Id",
    "Product Name",
    "Customer Name",
    "Start Date",
    "End Date",
    "Quantity",
    "Price",
    "Invoice Number",
)

IMPORT_SAMPLE_ROW: tuple[str, ...] = (
    "SUB-001",
    "Microsoft 365 Business Basic",
    "Acme Ltd",
    "2026-01-01",
    "2027-01-01",
    1,
    1200.00,
    "INV-001",
)

HEADER_ALIASES: dict[str, str] = {
    # Case export (exact headers normalize via _norm_header)
    "case_id": "case_id",
    "case_details": "product",
    "source": "source",
    "priority": "priority",
    "date_created": "start_date",
    "date_modified": "date_modified",
    "status": "status",
    "due_date": "end_date",
    "assignee": "assignee",
    "customer_category": "customer_category",
    "reference_id": "reference_id",
    "area": "area",
    "sub_area": "sub_area",
    "issue": "product_alt",
    "sub_issue": "sub_issue",
    "version": "version",
    "partner_company": "vendor",
    "contact_person": "client",
    "phone_num": "phone_num",
    "mobile_num": "mobile_num",
    "email": "email",
    "address": "address",
    "city": "city",
    "state": "state",
    "zip_code": "zip_code",
    "country": "country",
    "description": "product_alt2",
    "resolution": "resolution",
    "tags": "tags",
    "comments": "comments",
    # Legacy CSP / simple templates (still accepted)
    "orderid": "reference_id",
    "order_id": "reference_id",
    "subscription": "subscription_code",
    "materialnumber": "material_number",
    "material_number": "material_number",
    "materialdescription": "product",
    "material_description": "product",
    "qty": "quantity",
    "quantity": "quantity",
    "startdate": "start_date",
    "start_date": "start_date",
    "enddate": "end_date",
    "end_date": "end_date",
    "customername": "client",
    "customer_name": "client",
    "partnercode": "vendor",
    "partner_code": "vendor",
    "contractvalue": "billing_amount",
    "contract_value": "billing_amount",
    "client": "client",
    "customer": "client",
    "product": "product",
    "product_name": "product",
    "vendor": "vendor",
    "vendor_name": "vendor",
    "billing_amount": "billing_amount",
    "price": "billing_amount",
    "amount": "billing_amount",
    "invoice_number": "invoice_number",
    "invoicenumber": "invoice_number",
    "invoice_no": "invoice_number",
    "invoiceno": "invoice_number",
    "invoice": "invoice_number",
    "subscription_code": "subscription_code",
    "subscription_id": "subscription_code",
    "subscriptionid": "subscription_code",
    "case_id": "subscription_code",
    "product_name": "product",
    "notes": "notes",
}

NOTE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Source", "source"),
    ("Priority", "priority"),
    ("Date modified", "date_modified"),
    ("Assignee", "assignee"),
    ("Customer category", "customer_category"),
    ("Area", "area"),
    ("Sub area", "sub_area"),
    ("Sub issue", "sub_issue"),
    ("Version", "version"),
    ("Phone", "phone_num"),
    ("Mobile", "mobile_num"),
    ("Email", "email"),
    ("Address", "address"),
    ("City", "city"),
    ("State", "state"),
    ("Zip code", "zip_code"),
    ("Country", "country"),
    ("Resolution", "resolution"),
    ("Tags", "tags"),
    ("Comments", "comments"),
)

# Column-mapping step (fixed list shown to the user).
MAPPING_UI_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("subscription_code", "Subscription id", True),
    ("product", "Product name", True),
    ("client", "Customer name", True),
    ("start_date", "Start date", True),
    ("end_date", "End date", True),
    ("quantity", "Quantity", True),
    ("billing_amount", "Price", False),
    ("invoice_number", "Invoice number", False),
)

ALLOWED_FIELDS = frozenset(HEADER_ALIASES.values()) | frozenset(
    f for _, f in NOTE_FIELDS
)

CASE_STATUS_MAP: dict[str, str] = {
    "open": ClientSubscription.Status.ACTIVE,
    "new": ClientSubscription.Status.ACTIVE,
    "in progress": ClientSubscription.Status.ACTIVE,
    "in_progress": ClientSubscription.Status.ACTIVE,
    "active": ClientSubscription.Status.ACTIVE,
    "on hold": ClientSubscription.Status.PAUSED,
    "on_hold": ClientSubscription.Status.PAUSED,
    "paused": ClientSubscription.Status.PAUSED,
    "hold": ClientSubscription.Status.PAUSED,
    "resolved": ClientSubscription.Status.ENDED,
    "closed": ClientSubscription.Status.ENDED,
    "complete": ClientSubscription.Status.ENDED,
    "completed": ClientSubscription.Status.ENDED,
    "cancelled": ClientSubscription.Status.ENDED,
    "canceled": ClientSubscription.Status.ENDED,
    "ended": ClientSubscription.Status.ENDED,
}


def _norm_header(val: Any) -> str:
    return _norm_header_io(val)


def _cell_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return str(val).strip()


def _parse_date(val: Any) -> date | None:
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, (int, float)):
        try:
            whole = int(val)
            if 30000 <= whole <= 80000:
                return date(1899, 12, 30) + timedelta(days=whole)
        except (ValueError, OverflowError):
            pass
    s = _cell_str(val)
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%m/%d/%Y",
        "%d-%b-%Y",
        "%d-%b-%y",
        "%d %b %Y",
        "%d %b %y",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


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


def _parse_quantity(val: Any) -> int:
    if val is None or val == "":
        return 1
    if isinstance(val, float) and val == int(val):
        return max(1, int(val))
    if isinstance(val, int):
        return max(1, val)
    s = _cell_str(val)
    if not s:
        return 1
    try:
        return max(1, int(Decimal(s)))
    except (InvalidOperation, ValueError):
        return 1


def _parse_status(raw: Any) -> str:
    s = _cell_str(raw).lower()
    if not s:
        return ClientSubscription.Status.ACTIVE
    if s in CASE_STATUS_MAP:
        return CASE_STATUS_MAP[s]
    by_value = {v: v for v, _ in ClientSubscription.Status.choices}
    if s in by_value:
        return by_value[s]
    by_label = {label.lower(): v for v, label in ClientSubscription.Status.choices}
    return by_label.get(s, ClientSubscription.Status.ACTIVE)


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


def _first_value(get: Callable[[str], Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        val = _cell_str(get(field))
        if val:
            return val
    return ""


def _build_notes(get: Callable[[str], Any], existing_notes: str) -> str:
    lines: list[str] = []
    if existing_notes:
        lines.append(existing_notes.strip())
    for label, field in (
        ("Case details", "product"),
        ("Issue", "product_alt"),
        ("Description", "product_alt2"),
    ):
        val = _cell_str(get(field))
        if val:
            lines.append(f"{label}: {val}")
    for label, field in NOTE_FIELDS:
        val = _cell_str(get(field))
        if val:
            lines.append(f"{label}: {val}")
    return "\n".join(lines)


def _get_or_create_client(
    company: CompanyProfile,
    client_name: str,
    get: Callable[[str], Any],
    clients_by_lower: dict[str, Client],
) -> Client:
    key = client_name.strip().lower()
    existing = clients_by_lower.get(key)
    if existing:
        return existing

    email = _cell_str(get("email"))[:254]
    phone = (_cell_str(get("mobile_num")) or _cell_str(get("phone_num")))[:64]
    client = Client.objects.create(
        company=company,
        name=client_name.strip()[:255],
        email=email,
        phone=phone,
    )
    clients_by_lower[key] = client
    return client


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


def _get_or_create_product(
    company: CompanyProfile,
    product_name: str,
    material_number: str,
    products_by_lower: dict[str, Product],
    products_by_sku: dict[str, Product],
    vendor: Any | None = None,
) -> Product | None:
    if material_number:
        hit = products_by_sku.get(material_number.lower())
        if hit:
            return hit
    if product_name:
        hit = products_by_lower.get(product_name.lower())
        if hit:
            return hit

    name = (product_name or material_number).strip()
    if not name:
        return None

    sku = material_number.strip()[:64] if material_number else ""
    product = Product.objects.create(
        company=company,
        name=name[:255],
        sku=sku,
        vendor=vendor,
        asset_type="Software",
        category="Subscription",
    )
    products_by_lower[name.lower()] = product
    if sku:
        products_by_sku[sku.lower()] = product
    return product


def ensure_import_vendor(company: CompanyProfile, vendor_name: str) -> Vendor:
    vendors_by_lower = {v.name.strip().lower(): v for v in company.vendors.all()}
    return _get_or_create_vendor(company, vendor_name, vendors_by_lower)


def resolve_import_vendor_name(choice: str, other_name: str = "") -> str | None:
    key = (choice or "").strip().lower()
    if key == "redington":
        return "Redington"
    if key == "tata":
        return "Tata"
    if key == "other":
        name = (other_name or "").strip()
        return name if name else None
    return None


def validate_column_mapping(col_map: dict[str, int]) -> list[str]:
    """Return human-readable errors when required mappings are missing."""
    errors: list[str] = []
    for field_key, label, required in MAPPING_UI_FIELDS:
        if required and field_key not in col_map:
            errors.append(f"Map a column to {label}.")
    return errors


def column_mapping_from_post(post: Any) -> dict[str, int]:
    col_map: dict[str, int] = {}
    for key, _, _ in MAPPING_UI_FIELDS:
        raw = (post.get(f"map_{key}") or "").strip()
        if not raw:
            continue
        try:
            idx = int(raw)
        except ValueError:
            continue
        if idx >= 0:
            col_map[key] = idx
    return col_map


def suggest_column_mapping(header_row: list[Any]) -> dict[str, int]:
    detected = _build_column_map(header_row)
    return {key: detected[key] for key, _, _ in MAPPING_UI_FIELDS if key in detected}


_SUBSCRIPTION_HEADER_HINTS = frozenset(
    {
        "subscription",
        "customer",
        "product",
        "start",
        "end",
        "qty",
        "quantity",
        "case",
        "contact",
        "partner",
        "material",
        "date",
        "order",
    }
)


def detect_import_header(
    file_bytes: bytes, filename: str = ""
) -> tuple[int, list[Any]]:
    """Return (header_row_index, padded header row)."""
    return detect_header_row(
        file_bytes, filename, HEADER_ALIASES, _SUBSCRIPTION_HEADER_HINTS
    )


def _read_header_row_raw(file_bytes: bytes, filename: str = "") -> list[Any]:
    _, row = detect_import_header(file_bytes, filename)
    return row


def read_import_headers(file_bytes: bytes, filename: str = "") -> list[str]:
    _, row = detect_import_header(file_bytes, filename)
    return [
        _cell_str(cell) if _cell_str(cell) else f"Column {idx + 1}"
        for idx, cell in enumerate(row)
    ]


def count_subscription_data_rows(
    file_bytes: bytes, filename: str = "", header_row_index: int = 0
) -> int:
    """Rows after the detected header row."""
    try:
        rows = list(iter_spreadsheet_rows(file_bytes, filename))
    except Exception:
        logger.warning(
            "Could not count subscription import rows filename=%s",
            filename,
            exc_info=True,
        )
        return 0
    return max(0, len(rows) - header_row_index - 1)


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


def _import_subscription_rows(
    company: CompanyProfile,
    rows_iter: Iterator[tuple[Any, ...] | list[str]],
    column_mapping: dict[str, int] | None = None,
    import_vendor: Vendor | None = None,
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

    if column_mapping is not None:
        col_map = column_mapping
    else:
        col_map = _build_column_map(header_row)
    map_errors = validate_column_mapping(col_map)
    if map_errors:
        return 0, 0, [(0, map_errors[0])]

    if not data_rows:
        return 0, 0, [
            (
                0,
                "No data rows found below the header row. "
                "Check that the file has subscription rows under the column titles.",
            )
        ]

    clients_by_lower = {c.name.strip().lower(): c for c in company.clients.all()}
    products_by_lower = {p.name.strip().lower(): p for p in company.products.all()}
    products_by_sku = {
        p.sku.strip().lower(): p
        for p in company.products.all()
        if (p.sku or "").strip()
    }
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

        client_name = _cell_str(get("client"))
        product_name = _cell_str(get("product"))
        sub_code = _cell_str(get("subscription_code"))
        if not client_name and not product_name and not sub_code:
            skipped_blank += 1
            continue
        if not client_name:
            errors.append((row_num, "Customer name is required."))
            continue
        if not product_name:
            errors.append((row_num, "Product name is required."))
            continue

        client = _get_or_create_client(company, client_name, get, clients_by_lower)

        product = _get_or_create_product(
            company,
            product_name,
            "",
            products_by_lower,
            products_by_sku,
            vendor=import_vendor,
        )
        if not product:
            errors.append((row_num, "Product name is required."))
            continue

        start_d = _parse_date(get("start_date"))
        if not start_d:
            errors.append(
                (
                    row_num,
                    "Start date is missing or invalid (use YYYY-MM-DD or DD/MM/YYYY).",
                )
            )
            continue

        end_d = _parse_date(get("end_date"))
        billing_amount = _parse_decimal(get("billing_amount"))
        if not sub_code:
            sub_code = f"ROW-{row_num}"

        existing = ClientSubscription.objects.filter(
            company=company,
            subscription_code__iexact=sub_code[:64],
        ).first()
        if existing:
            existing.client = client
            existing.product = product
            existing.vendor = import_vendor
            existing.start_date = start_d
            existing.end_date = end_d
            existing.quantity = _parse_quantity(get("quantity"))
            if "billing_amount" in col_map:
                existing.billing_amount = billing_amount
            if "invoice_number" in col_map:
                existing.invoice_number = _cell_str(get("invoice_number"))[:128]
            existing.save()
            updated += 1
            continue

        ClientSubscription.objects.create(
            company=company,
            client=client,
            product=product,
            vendor=import_vendor,
            status=ClientSubscription.Status.ACTIVE,
            start_date=start_d,
            end_date=end_d,
            quantity=_parse_quantity(get("quantity")),
            billing_amount=billing_amount,
            invoice_number=_cell_str(get("invoice_number"))[:128],
            serial_number="",
            subscription_code=sub_code[:64],
            notes="",
        )
        created += 1

    if created == 0 and updated == 0 and not errors:
        if skipped_blank:
            errors.append(
                (
                    0,
                    f"All {skipped_blank} data row(s) look empty for the columns you mapped. "
                    "Re-check Customer name, Product name, and Start date column selections.",
                )
            )
        else:
            errors.append(
                (
                    0,
                    "No subscription rows could be imported from this file.",
                )
            )

    return created, updated, errors


def import_subscriptions_from_file(
    company: CompanyProfile,
    file_bytes: bytes,
    filename: str = "",
    column_mapping: dict[str, int] | None = None,
    import_vendor: Vendor | None = None,
    header_row_index: int = 0,
) -> tuple[int, int, list[tuple[int, str]]]:
    """
    Import subscriptions from .xlsx or .csv file bytes.
    Returns (created_count, updated_count, list of (row_number, error_message)).
    """
    if not file_bytes:
        return 0, 0, [(0, "Empty file.")]

    lower = (filename or "").lower()
    if not (lower.endswith(".csv") or lower.endswith(".xlsx")):
        return 0, 0, [(0, "Unsupported file type. Upload a .xlsx or .csv file.")]

    try:
        _, header_row = detect_import_header(file_bytes, filename)
        min_cols = len(header_row)
        rows_iter = iter_spreadsheet_rows(
            file_bytes, filename, min_cols=min_cols
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not read subscription import file %s: %s",
            filename,
            exc,
            extra={"company_id": company.pk},
            exc_info=True,
        )
        kind = "CSV" if lower.endswith(".csv") else "Excel"
        return 0, 0, [(0, f"Could not read {kind} file: {exc}")]

    return _import_subscription_rows(
        company,
        rows_iter,
        column_mapping=column_mapping,
        import_vendor=import_vendor,
        header_row_index=header_row_index,
    )


def import_subscriptions_from_xlsx(
    company: CompanyProfile,
    file_bytes: bytes,
) -> tuple[int, int, list[tuple[int, str]]]:
    """Backward-compatible entry point for Excel-only imports."""
    return import_subscriptions_from_file(company, file_bytes, "import.xlsx")
