"""Export and import clients for bulk editing."""

from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from accounts.models import CompanyProfile
from business.import_io import cell_str, detect_header_row, iter_spreadsheet_rows, norm_header
from business.models import Client
from openpyxl import Workbook

EXPORT_HEADERS: tuple[str, ...] = (
    "Client ID",
    "Name",
    "GST Number",
    "Email 1",
    "Email 2",
    "Email 3",
    "Contact 1",
    "Contact 2",
    "Contact 3",
    "Notes",
)

HEADER_ALIASES: dict[str, str] = {
    "client_id": "client_id",
    "id": "client_id",
    "name": "name",
    "client_name": "name",
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
    "contact": "phone",
    "contact_1": "phone",
    "contact1": "phone",
    "phone": "phone",
    "phone_1": "phone",
    "phone1": "phone",
    "contact_2": "phone_2",
    "contact2": "phone_2",
    "phone_2": "phone_2",
    "phone2": "phone_2",
    "contact_3": "phone_3",
    "contact3": "phone_3",
    "phone_3": "phone_3",
    "phone3": "phone_3",
    "notes": "notes",
}

HINT_WORDS = frozenset({"client", "name", "gst", "email", "contact", "phone", "notes"})
_UNSET = object()


def build_client_export_bytes(company: CompanyProfile) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Clients"
    ws.append(list(EXPORT_HEADERS))
    for client in company.clients.all().order_by("name"):
        ws.append(
            [
                client.pk,
                client.name,
                client.gst_number,
                client.email,
                client.email_2,
                client.email_3,
                client.phone,
                client.phone_2,
                client.phone_3,
                client.notes,
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
    if "name" not in col_map and "client_id" not in col_map:
        return ["The file must include a Client ID or Name column."]
    return []


def _parse_client_id(raw: Any) -> int | None:
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
        return None, f"Invalid email address: {email}"
    return email, None


def _mapped_email(get, col_map: dict[str, int], field: str) -> tuple[Any, str | None]:
    if field not in col_map:
        return _UNSET, None
    return _parse_email(get(field))


def _mapped_text(get, col_map: dict[str, int], field: str, maxlen: int) -> Any:
    if field not in col_map:
        return _UNSET
    return cell_str(get(field))[:maxlen]


def _upsert_client(
    company: CompanyProfile,
    get,
    clients_by_id: dict[int, Client],
    clients_by_name: dict[str, Client],
    col_map: dict[str, int],
) -> tuple[Client | None, str | None, bool]:
    client_id = _parse_client_id(get("client_id"))
    name = cell_str(get("name"))
    gst_number = cell_str(get("gst_number"))[:32]
    email, email_err = _mapped_email(get, col_map, "email")
    if email_err:
        return None, email_err, False
    email_2, email_2_err = _mapped_email(get, col_map, "email_2")
    if email_2_err:
        return None, email_2_err, False
    email_3, email_3_err = _mapped_email(get, col_map, "email_3")
    if email_3_err:
        return None, email_3_err, False
    phone = _mapped_text(get, col_map, "phone", 64)
    phone_2 = _mapped_text(get, col_map, "phone_2", 64)
    phone_3 = _mapped_text(get, col_map, "phone_3", 64)
    notes = cell_str(get("notes"))

    existing: Client | None = None
    if client_id is not None:
        existing = clients_by_id.get(client_id)
        if existing is None:
            return None, f"Client ID {client_id} was not found for your company.", False

    if existing is None and name:
        existing = clients_by_name.get(name.lower())

    if existing is None:
        if not name:
            return None, "Name is required for new clients.", False
        client = Client.objects.create(
            company=company,
            name=name,
            gst_number=gst_number,
            email="" if email is _UNSET else (email or ""),
            email_2="" if email_2 is _UNSET else (email_2 or ""),
            email_3="" if email_3 is _UNSET else (email_3 or ""),
            phone="" if phone is _UNSET else phone,
            phone_2="" if phone_2 is _UNSET else phone_2,
            phone_3="" if phone_3 is _UNSET else phone_3,
            notes=notes,
        )
        clients_by_id[client.pk] = client
        clients_by_name[name.lower()] = client
        return client, None, True

    if name:
        existing.name = name
    existing.gst_number = gst_number
    if email is not _UNSET:
        existing.email = email or ""
    if email_2 is not _UNSET:
        existing.email_2 = email_2 or ""
    if email_3 is not _UNSET:
        existing.email_3 = email_3 or ""
    if phone is not _UNSET:
        existing.phone = phone
    if phone_2 is not _UNSET:
        existing.phone_2 = phone_2
    if phone_3 is not _UNSET:
        existing.phone_3 = phone_3
    existing.notes = notes
    existing.save()
    clients_by_id[existing.pk] = existing
    clients_by_name[existing.name.strip().lower()] = existing
    return existing, None, False


def _import_client_rows(
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
        return 0, 0, [
            (
                0,
                "No data rows found below the header row.",
            )
        ]

    clients_by_id = {c.pk: c for c in company.clients.all()}
    clients_by_name = {c.name.strip().lower(): c for c in company.clients.all()}
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

        if not cell_str(get("name")) and _parse_client_id(get("client_id")) is None:
            skipped_blank += 1
            continue

        _client, err, was_created = _upsert_client(
            company, get, clients_by_id, clients_by_name, col_map
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
                    "Include Client ID or Name on each row you want to update.",
                )
            )
        else:
            errors.append((0, "No client rows could be imported from this file."))

    return created, updated, errors


def import_clients_from_file(
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
    return _import_client_rows(
        company,
        rows,
        header_row_index=header_row_index,
    )
