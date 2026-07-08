"""Export and import vendors for bulk editing."""

from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email

from accounts.models import CompanyProfile
from business.import_io import cell_str, detect_header_row, iter_spreadsheet_rows, norm_header
from business.models import Vendor
from openpyxl import Workbook

EXPORT_HEADERS: tuple[str, ...] = (
    "Vendor ID",
    "Name",
    "GST Number",
    "Email 1",
    "Email 2",
    "Email 3",
    "Phone 1",
    "Phone 2",
    "Phone 3",
    "Website",
    "Notes",
)

HEADER_ALIASES: dict[str, str] = {
    "vendor_id": "vendor_id",
    "id": "vendor_id",
    "name": "name",
    "vendor_name": "name",
    "gst_number": "gst_number",
    "gst": "gst_number",
    "gst_no": "gst_number",
    "email": "email",
    "email_1": "email",
    "email1": "email",
    "email_2": "email_2",
    "email2": "email_2",
    "email_3": "email_3",
    "email3": "email_3",
    "phone": "phone",
    "phone_1": "phone",
    "phone1": "phone",
    "contact": "phone",
    "phone_2": "phone_2",
    "phone2": "phone_2",
    "phone_3": "phone_3",
    "phone3": "phone_3",
    "website": "website",
    "notes": "notes",
}

HINT_WORDS = frozenset(
    {"vendor", "name", "gst", "email", "phone", "contact", "website", "notes"}
)


def build_vendor_export_bytes(company: CompanyProfile) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Vendors"
    ws.append(list(EXPORT_HEADERS))
    for vendor in company.vendors.all().order_by("name"):
        ws.append(
            [
                vendor.pk,
                vendor.name,
                vendor.gst_number,
                vendor.email,
                vendor.email_2,
                vendor.email_3,
                vendor.phone,
                vendor.phone_2,
                vendor.phone_3,
                vendor.website,
                vendor.notes,
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
    if "name" not in col_map and "vendor_id" not in col_map:
        return ["The file must include a Vendor ID or Name column."]
    return []


def _parse_vendor_id(raw: Any) -> int | None:
    text = cell_str(raw)
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _parse_email(raw: Any, label: str) -> tuple[str | None, str | None]:
    email = cell_str(raw)
    if not email:
        return "", None
    try:
        validate_email(email)
    except ValidationError:
        return None, f"Invalid {label}: {email}"
    return email, None


def _parse_website(raw: Any) -> tuple[str | None, str | None]:
    website = cell_str(raw)
    if not website:
        return "", None
    if "://" not in website:
        website = f"https://{website}"
    try:
        URLValidator()(website)
    except ValidationError:
        return None, f"Invalid website URL: {cell_str(raw)}"
    return website, None


def _upsert_vendor(
    company: CompanyProfile,
    get,
    vendors_by_id: dict[int, Vendor],
    vendors_by_name: dict[str, Vendor],
) -> tuple[Vendor | None, str | None, bool]:
    vendor_id = _parse_vendor_id(get("vendor_id"))
    name = cell_str(get("name"))
    gst_number = cell_str(get("gst_number"))[:32]

    email, email_err = _parse_email(get("email"), "email 1")
    if email_err:
        return None, email_err, False
    email_2, email_2_err = _parse_email(get("email_2"), "email 2")
    if email_2_err:
        return None, email_2_err, False
    email_3, email_3_err = _parse_email(get("email_3"), "email 3")
    if email_3_err:
        return None, email_3_err, False

    phone = cell_str(get("phone"))[:64]
    phone_2 = cell_str(get("phone_2"))[:64]
    phone_3 = cell_str(get("phone_3"))[:64]
    website, website_err = _parse_website(get("website"))
    if website_err:
        return None, website_err, False
    notes = cell_str(get("notes"))

    existing: Vendor | None = None
    if vendor_id is not None:
        existing = vendors_by_id.get(vendor_id)
        if existing is None:
            return None, f"Vendor ID {vendor_id} was not found for your company.", False

    if existing is None and name:
        existing = vendors_by_name.get(name.lower())

    if existing is None:
        if not name:
            return None, "Name is required for new vendors.", False
        vendor = Vendor.objects.create(
            company=company,
            name=name,
            gst_number=gst_number,
            email=email or "",
            email_2=email_2 or "",
            email_3=email_3 or "",
            phone=phone,
            phone_2=phone_2,
            phone_3=phone_3,
            website=website or "",
            notes=notes,
        )
        vendors_by_id[vendor.pk] = vendor
        vendors_by_name[name.lower()] = vendor
        return vendor, None, True

    if name:
        existing.name = name
    existing.gst_number = gst_number
    existing.email = email or ""
    existing.email_2 = email_2 or ""
    existing.email_3 = email_3 or ""
    existing.phone = phone
    existing.phone_2 = phone_2
    existing.phone_3 = phone_3
    existing.website = website or ""
    existing.notes = notes
    existing.save()
    vendors_by_id[existing.pk] = existing
    vendors_by_name[existing.name.strip().lower()] = existing
    return existing, None, False


def _import_vendor_rows(
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

    vendors_by_id = {v.pk: v for v in company.vendors.all()}
    vendors_by_name = {v.name.strip().lower(): v for v in company.vendors.all()}
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

        if not cell_str(get("name")) and _parse_vendor_id(get("vendor_id")) is None:
            skipped_blank += 1
            continue

        _vendor, err, was_created = _upsert_vendor(
            company, get, vendors_by_id, vendors_by_name
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
                    "Include Vendor ID or Name on each row you want to update.",
                )
            )
        else:
            errors.append((0, "No vendor rows could be imported from this file."))

    return created, updated, errors


def import_vendors_from_file(
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
    return _import_vendor_rows(
        company,
        rows,
        header_row_index=header_row_index,
    )
