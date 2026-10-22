"""Read Zoho Books invoices and turn each product line into a subscription draft."""

from __future__ import annotations

import calendar
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher

import requests
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import CompanyProfile
from business.models import (
    ClientSubscription,
    ZohoBooksDocument,
    ZohoBooksDocumentLine,
    ZohoBooksSync,
    _add_one_year,
)
from business.zoho_crm import api_base_url, get_access_token

logger = logging.getLogger(__name__)

SESSION_INVOICE_IDS = "zoho_books_invoice_ids"
SESSION_INVOICE_CACHE = "zoho_books_invoice_page_cache"
SYNC_PAGE_SIZE = 20
STORED_PAGE_SIZE = 50
# Keep Zoho under its per-minute request cap while a sync is running.
_MIN_REQUEST_GAP_SECONDS = 0.7
_rate_lock = threading.Lock()
_next_books_request = 0.0


class ZohoBooksError(RuntimeError):
    def __init__(self, message: str, *, needs_reconnect: bool = False):
        super().__init__(message)
        self.needs_reconnect = needs_reconnect


def books_api_root(company: CompanyProfile) -> str:
    domain = (company.zoho_api_domain or "").strip().rstrip("/") or api_base_url()
    return f"{domain}/books/v3"


def _response_json(resp) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _needs_reconnect(resp, body: dict) -> bool:
    if resp.status_code in (401, 403):
        return True
    message = str(body.get("message") or "").lower()
    return "scope" in message or "not authorized" in message


def _pace_books_request() -> None:
    """Serialize Zoho Books calls so a full bill sync does not trip the rate limit."""
    global _next_books_request
    with _rate_lock:
        now = time.monotonic()
        wait = _next_books_request - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _next_books_request = now + _MIN_REQUEST_GAP_SECONDS


def books_get(company: CompanyProfile, path: str, params=None, *, accept: str = ""):
    _pace_books_request()
    headers = {"Authorization": f"Zoho-oauthtoken {get_access_token(company)}"}
    if accept:
        headers["Accept"] = accept
    url = f"{books_api_root(company)}/{path.lstrip('/')}"
    try:
        resp = requests.get(url, headers=headers, params=params or {}, timeout=45)
    except requests.RequestException:
        logger.exception("Zoho Books request failed company_id=%s path=%s", company.pk, path)
        raise ZohoBooksError("Could not reach Zoho Books. Try again.") from None
    if accept == "application/pdf":
        if resp.ok and resp.content.startswith(b"%PDF"):
            return resp
        body = _response_json(resp)
        if _needs_reconnect(resp, body):
            raise ZohoBooksError(
                "Zoho Books permission is missing. In Company profile, disconnect Zoho, "
                "connect again, and accept the Books permissions.",
                needs_reconnect=True,
            )
        raise ZohoBooksError(
            body.get("message") or "Could not download the invoice PDF from Zoho Books."
        )
    body = _response_json(resp)
    if not resp.ok or body.get("code") not in (None, 0):
        if _needs_reconnect(resp, body):
            raise ZohoBooksError(
                "Zoho Books permission is missing. In Company profile, disconnect Zoho, "
                "connect again, and accept the Books permissions.",
                needs_reconnect=True,
            )
        raise ZohoBooksError(
            body.get("message") or f"Zoho Books request failed ({resp.status_code})."
        )
    return resp


def list_organizations(company: CompanyProfile) -> list[dict]:
    resp = books_get(company, "organizations")
    return list(_response_json(resp).get("organizations") or [])


def resolve_organization(company: CompanyProfile) -> dict | None:
    """Return the saved Books organization, or the only one on the account."""
    organizations = list_organizations(company)
    by_id = {str(org.get("organization_id") or ""): org for org in organizations}
    by_id.pop("", None)
    saved = (company.zoho_books_organization_id or "").strip()
    if saved and saved in by_id:
        return by_id[saved]
    if len(by_id) == 1:
        org = next(iter(by_id.values()))
        company.zoho_books_organization_id = str(org.get("organization_id") or "")
        company.zoho_books_organization_name = (org.get("name") or "")[:255]
        company.save(
            update_fields=[
                "zoho_books_organization_id",
                "zoho_books_organization_name",
                "updated_at",
            ]
        )
        return org
    return None


def save_organization(company: CompanyProfile, organization_id: str) -> dict:
    organization_id = (organization_id or "").strip()
    organizations = list_organizations(company)
    for org in organizations:
        if str(org.get("organization_id") or "") == organization_id:
            company.zoho_books_organization_id = organization_id
            company.zoho_books_organization_name = (org.get("name") or "")[:255]
            company.save(
                update_fields=[
                    "zoho_books_organization_id",
                    "zoho_books_organization_name",
                    "updated_at",
                ]
            )
            return org
    raise ZohoBooksError("Choose a Zoho Books organization from the list.")


def list_invoices(
    company: CompanyProfile,
    organization_id: str,
    *,
    page: int = 1,
    invoice_number: str = "",
    item_id: str = "",
    per_page: int = 50,
    date_start: str = "",
    date_end: str = "",
    modified_after=None,
) -> tuple[list[dict], dict]:
    params = {
        "organization_id": organization_id,
        "sort_column": "date",
        "sort_order": "D",
        "per_page": max(1, min(int(per_page), 200)),
        "page": max(page, 1),
    }
    query = (invoice_number or "").strip()
    if query:
        params["invoice_number_contains"] = query
    item_id = (item_id or "").strip()
    if item_id:
        params["item_id"] = item_id
    if date_start:
        params["date_start"] = date_start
    if date_end:
        params["date_end"] = date_end
    if modified_after is not None:
        params["last_modified_time"] = _format_zoho_time(modified_after)
    resp = books_get(company, "invoices", params)
    body = _response_json(resp)
    return list(body.get("invoices") or []), dict(body.get("page_context") or {})


def list_items(company: CompanyProfile, organization_id: str) -> list[dict]:
    """Active Zoho Books items, used as the invoice product filter."""
    get_access_token(company)
    items = []
    page = 1
    while page <= 15:
        resp = books_get(
            company,
            "items",
            {
                "organization_id": organization_id,
                "page": page,
                "per_page": 200,
            },
        )
        body = _response_json(resp)
        for item in body.get("items") or []:
            item_id = str(item.get("item_id") or "").strip()
            name = (item.get("name") or "").strip()
            status = str(item.get("status") or "active").strip().lower()
            if not item_id or not name or status == "inactive":
                continue
            items.append(
                {
                    "item_id": item_id,
                    "name": name,
                    "sku": (item.get("sku") or "").strip(),
                }
            )
        if not (body.get("page_context") or {}).get("has_more_page"):
            break
        page += 1
    items.sort(key=lambda item: item["name"].casefold())
    return items


_MAX_ITEM_PAGES = 8


def _collect_invoices_for_item(
    company: CompanyProfile,
    organization_id: str,
    item_id: str,
    invoice_number: str,
) -> tuple[list[dict], bool]:
    found = []
    truncated = False
    page = 1
    while page <= _MAX_ITEM_PAGES:
        invoices, page_context = list_invoices(
            company,
            organization_id,
            page=page,
            invoice_number=invoice_number,
            item_id=item_id,
            per_page=200,
        )
        found.extend(invoices)
        if not page_context.get("has_more_page"):
            break
        if page == _MAX_ITEM_PAGES:
            truncated = True
            break
        page += 1
    return found, truncated


def list_invoices_for_items(
    company: CompanyProfile,
    organization_id: str,
    item_ids: list[str],
    *,
    page: int = 1,
    invoice_number: str = "",
    per_page: int = 50,
) -> tuple[list[dict], dict]:
    """Invoices that contain any of the selected Zoho items."""
    selected = []
    seen_items = set()
    for item_id in item_ids:
        item_id = str(item_id).strip()
        if item_id.isdigit() and item_id not in seen_items:
            seen_items.add(item_id)
            selected.append(item_id)
    item_ids = selected
    if not item_ids:
        return list_invoices(
            company,
            organization_id,
            page=page,
            invoice_number=invoice_number,
            per_page=per_page,
        )

    get_access_token(company)
    merged: dict[str, dict] = {}
    truncated = False
    workers = min(6, len(item_ids))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _collect_invoices_for_item,
                company,
                organization_id,
                item_id,
                invoice_number,
            )
            for item_id in item_ids
        ]
        for future in as_completed(futures):
            invoices, item_truncated = future.result()
            truncated = truncated or item_truncated
            for invoice in invoices:
                invoice_id = str(invoice.get("invoice_id") or "").strip()
                if invoice_id:
                    merged[invoice_id] = invoice
    rows = sorted(
        merged.values(),
        key=lambda invoice: (
            str(invoice.get("date") or ""),
            str(invoice.get("invoice_number") or ""),
        ),
        reverse=True,
    )
    page = max(page, 1)
    start = (page - 1) * per_page
    return rows[start : start + per_page], {
        "has_more_page": start + per_page < len(rows),
        "truncated": truncated,
    }


def get_invoice(company: CompanyProfile, organization_id: str, invoice_id: str) -> dict:
    resp = books_get(
        company,
        f"invoices/{invoice_id}",
        {"organization_id": organization_id},
    )
    invoice = _response_json(resp).get("invoice")
    if not isinstance(invoice, dict):
        raise ZohoBooksError("Zoho Books did not return that invoice.")
    return invoice


def get_invoice_pdf(company: CompanyProfile, organization_id: str, invoice_id: str) -> bytes:
    resp = books_get(
        company,
        f"invoices/{invoice_id}",
        {"organization_id": organization_id, "accept": "pdf"},
        accept="application/pdf",
    )
    return resp.content


def local_organization(company: CompanyProfile) -> dict | None:
    """Saved Books organization, without calling Zoho."""
    saved = (company.zoho_books_organization_id or "").strip()
    if not saved:
        return None
    name = (company.zoho_books_organization_name or "").strip() or "Zoho Books"
    return {"organization_id": saved, "name": name}


def list_bills(
    company: CompanyProfile,
    organization_id: str,
    *,
    page: int = 1,
    per_page: int = 50,
    modified_after=None,
) -> tuple[list[dict], dict]:
    params = {
        "organization_id": organization_id,
        "sort_column": "date",
        "sort_order": "D",
        "per_page": max(1, min(int(per_page), 200)),
        "page": max(page, 1),
    }
    if modified_after is not None:
        params["last_modified_time"] = _format_zoho_time(modified_after)
    resp = books_get(company, "bills", params)
    body = _response_json(resp)
    return list(body.get("bills") or []), dict(body.get("page_context") or {})


def get_bill(company: CompanyProfile, organization_id: str, bill_id: str) -> dict:
    resp = books_get(
        company,
        f"bills/{bill_id}",
        {"organization_id": organization_id},
    )
    bill = _response_json(resp).get("bill")
    if not isinstance(bill, dict):
        raise ZohoBooksError("Zoho Books did not return that bill.")
    return bill


def get_bill_pdf(company: CompanyProfile, organization_id: str, bill_id: str) -> bytes:
    resp = books_get(
        company,
        f"bills/{bill_id}",
        {"organization_id": organization_id, "accept": "pdf"},
        accept="application/pdf",
    )
    return resp.content


def _format_zoho_time(value) -> str:
    aware = value if timezone.is_aware(value) else timezone.make_aware(value, timezone.utc)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _document_id(kind: str, payload: dict) -> str:
    key = "invoice_id" if kind == ZohoBooksSync.Kind.INVOICE else "bill_id"
    return str(payload.get(key) or "").strip()


def _document_number(kind: str, payload: dict) -> str:
    key = "invoice_number" if kind == ZohoBooksSync.Kind.INVOICE else "bill_number"
    return (payload.get(key) or "")[:128]


def _document_party(kind: str, payload: dict) -> str:
    key = "customer_name" if kind == ZohoBooksSync.Kind.INVOICE else "vendor_name"
    return (payload.get(key) or "")[:255]


def _optional_money(payload: dict, key: str):
    if key not in payload or payload.get(key) in (None, ""):
        return None
    return _money(payload.get(key))


def list_documents_page(
    company: CompanyProfile,
    organization_id: str,
    kind: str,
    *,
    page: int,
    modified_after=None,
) -> tuple[list[dict], dict]:
    if kind == ZohoBooksSync.Kind.BILL:
        return list_bills(
            company,
            organization_id,
            page=page,
            per_page=SYNC_PAGE_SIZE,
            modified_after=modified_after,
        )
    return list_invoices(
        company,
        organization_id,
        page=page,
        per_page=SYNC_PAGE_SIZE,
        modified_after=modified_after,
    )


def normalize_gst(value: str) -> str:
    return re.sub(r"\s+", "", (value or "")).upper()


def _parse_date(value: str):
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


_DURATION_RE = re.compile(
    r"duration\s*:\s*"
    r"(\d{1,2}[-/]\d{1,2}[-/]\d{4})"
    r"\s*(?:to|–|—|-)\s*"
    r"(\d{1,2}[-/]\d{1,2}[-/]\d{4})",
    re.IGNORECASE,
)
_SERIAL_IN_DESCRIPTION_RE = re.compile(
    r"S\s*/\s*N\s*:\s*-?\s*([^\r\n]+)",
    re.IGNORECASE,
)


def _parse_dmy(value: str):
    text = (value or "").strip().replace("/", "-")
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def duration_dates(text: str):
    """Return start and end when a line description contains Duration: date TO date."""
    match = _DURATION_RE.search(text or "")
    if not match:
        return None
    start = _parse_dmy(match.group(1))
    end = _parse_dmy(match.group(2))
    if start is None or end is None:
        return None
    return start, end


def _money(value) -> Decimal:
    try:
        amount = Decimal(str(value if value not in (None, "") else "0"))
    except (InvalidOperation, ValueError):
        amount = Decimal("0")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _quantity(value) -> int:
    try:
        number = Decimal(str(value if value not in (None, "") else "1"))
    except (InvalidOperation, ValueError):
        return 1
    qty = int(number.to_integral_value(rounding=ROUND_HALF_UP))
    return max(qty, 1)


def _serial_from_description(text: str) -> str:
    """Serial written in the product description as S/N: value."""
    parts = []
    for match in _SERIAL_IN_DESCRIPTION_RE.finditer(text or ""):
        serial = match.group(1).strip()
        if serial:
            parts.append(serial)
    return ", ".join(parts)[:128]


def _serial_number(line: dict) -> str:
    from_description = _serial_from_description(line.get("description") or "")
    if from_description:
        return from_description
    raw = line.get("serial_numbers") or []
    if isinstance(raw, str):
        raw = [raw]
    parts: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            text = item.get("serial_number") or item.get("serialnumber") or ""
        else:
            text = str(item or "")
        text = text.strip()
        if text:
            parts.append(text)
    return ", ".join(parts)[:128]


def _line_name(line: dict) -> str:
    name = (line.get("name") or "").strip()
    if name:
        return name
    return (line.get("description") or "").strip()[:255]


def slim_invoice(invoice: dict) -> dict:
    """Invoice fields kept until the next page refresh."""
    items = []
    for line in invoice.get("line_items") or []:
        name = _line_name(line)
        if not name:
            continue
        item_id = str(line.get("item_id") or "").strip() or name.casefold()
        items.append(
            {
                "item_id": item_id,
                "name": name,
                "sku": (line.get("sku") or "").strip(),
                "quantity": _quantity(line.get("quantity")),
                "amount": str(_money(line.get("item_total"))),
            }
        )
    total = invoice.get("total")
    return {
        "invoice_id": str(invoice.get("invoice_id") or "").strip(),
        "invoice_number": invoice.get("invoice_number") or "",
        "date": invoice.get("date") or "",
        "customer_name": invoice.get("customer_name") or "",
        "status": invoice.get("status") or "",
        "currency_code": invoice.get("currency_code") or "",
        "total": "" if total is None else str(total),
        "items_loaded": "line_items" in invoice,
        "items": items,
    }


def products_on_invoices(invoices: list[dict]) -> list[dict]:
    """Unique products found on the invoices already loaded."""
    found = {}
    for invoice in invoices:
        for item in invoice.get("items") or []:
            item_id = str(item.get("item_id") or "").strip()
            name = (item.get("name") or "").strip()
            if not item_id or not name or item_id in found:
                continue
            found[item_id] = {
                "item_id": item_id,
                "name": name,
                "sku": (item.get("sku") or "").strip(),
            }
    return sorted(found.values(), key=lambda item: item["name"].casefold())


def invoice_period_range(year: str, month: str = "") -> tuple[str, str] | None:
    """Invoice date range for a year, or one month inside that year."""
    year_text = (year or "").strip()
    if not year_text.isdigit():
        return None
    year_number = int(year_text)
    if year_number < 2000 or year_number > 2100:
        return None
    month_text = (month or "").strip()
    if not month_text:
        return f"{year_number:04d}-01-01", f"{year_number:04d}-12-31"
    if not month_text.isdigit():
        return None
    month_number = int(month_text)
    if month_number < 1 or month_number > 12:
        return None
    last_day = calendar.monthrange(year_number, month_number)[1]
    return (
        f"{year_number:04d}-{month_number:02d}-01",
        f"{year_number:04d}-{month_number:02d}-{last_day:02d}",
    )


def count_invoices(
    company: CompanyProfile,
    organization_id: str,
    *,
    date_start: str = "",
    date_end: str = "",
) -> int:
    """How many invoices fall in the date range, without loading product lines."""
    total = 0
    page = 1
    while page <= 25:
        invoices, page_context = list_invoices(
            company,
            organization_id,
            page=page,
            per_page=200,
            date_start=date_start,
            date_end=date_end,
        )
        reported = page_context.get("total")
        if reported is not None and str(reported).isdigit():
            return int(reported)
        total += len(invoices)
        if not page_context.get("has_more_page") or not invoices:
            break
        page += 1
    return total


def load_invoice_page(
    company: CompanyProfile,
    organization_id: str,
    *,
    page: int = 1,
    invoice_number: str = "",
    date_start: str = "",
    date_end: str = "",
) -> tuple[list[dict], bool]:
    """Load one invoice page and the product lines on each invoice."""
    summaries, page_context = list_invoices(
        company,
        organization_id,
        page=page,
        invoice_number=invoice_number,
        date_start=date_start,
        date_end=date_end,
    )
    get_access_token(company)
    detailed: dict[str, dict] = {}

    def fetch_one(summary: dict) -> tuple[str, dict]:
        invoice_id = str(summary.get("invoice_id") or "").strip()
        if not invoice_id:
            return "", slim_invoice(summary)
        try:
            full = get_invoice(company, organization_id, invoice_id)
        except ZohoBooksError:
            logger.warning(
                "Zoho invoice detail failed company_id=%s invoice_id=%s",
                company.pk,
                invoice_id,
            )
            full = summary
        return invoice_id, slim_invoice(full)

    workers = min(6, len(summaries)) or 1
    if summaries:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fetch_one, summary) for summary in summaries]
            for future in as_completed(futures):
                invoice_id, invoice = future.result()
                if invoice_id:
                    detailed[invoice_id] = invoice
    invoices = []
    for summary in summaries:
        invoice_id = str(summary.get("invoice_id") or "").strip()
        invoices.append(detailed.get(invoice_id) or slim_invoice(summary))
    return invoices, bool(page_context.get("has_more_page"))


def invoice_item_rows(invoice: dict) -> list[dict]:
    """Product lines shown when an invoice row is expanded."""
    rows = []
    for line in invoice.get("line_items") or []:
        name = _line_name(line)
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "quantity": _quantity(line.get("quantity")),
                "amount": str(_money(line.get("item_total"))),
            }
        )
    return rows


def _normalize_client_name(value: str) -> str:
    text = (value or "").casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\bprivate\b", "pvt", text)
    text = re.sub(r"\blimited\b", "ltd", text)
    return re.sub(r"\s+", " ", text).strip()


def _match_client(clients, customer_name: str, gst_no: str):
    gst = normalize_gst(gst_no)
    if gst:
        matches = [client for client in clients if normalize_gst(client.gst_number) == gst]
        if len(matches) == 1:
            return matches[0], "Client matched by GST."
    wanted = _normalize_client_name(customer_name)
    if not wanted:
        return None, "Select the client. This Zoho customer was not found in the app."
    scored = []
    for client in clients:
        normalized = _normalize_client_name(client.name)
        if not normalized:
            continue
        if normalized == wanted:
            score = 1.0
        else:
            score = SequenceMatcher(None, wanted, normalized).ratio()
        scored.append((score, client))
    if not scored:
        return None, "Select the client. This Zoho customer was not found in the app."
    scored.sort(key=lambda item: item[0], reverse=True)
    exact = [client for score, client in scored if score == 1.0]
    if len(exact) == 1:
        return exact[0], "Client matched by name."
    if len(exact) > 1:
        return None, "Select the client. More than one client name is equally close."
    best_score, best_client = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if best_score >= 0.86 and best_score - second_score >= 0.04:
        return best_client, "Client matched by closest name."
    return None, "Select the client. This Zoho customer was not found in the app."


def _match_product(products, line: dict):
    sku = (line.get("sku") or "").strip().casefold()
    if sku:
        matches = [product for product in products if (product.sku or "").strip().casefold() == sku]
        if len(matches) == 1:
            return matches[0], "Product matched by SKU."
    name = _line_name(line).casefold()
    if name:
        matches = [product for product in products if (product.name or "").strip().casefold() == name]
        if len(matches) == 1:
            return matches[0], "Product matched by name."
    return None, "Select the product. This Zoho item was not found in the app."


def _vendor_for_product(product):
    if product is None:
        return None
    if product.vendor_id:
        return product.vendor
    vendors = list(product.vendors.all())
    if len(vendors) == 1:
        return vendors[0]
    return None


def mark_imported_invoices(company: CompanyProfile, invoices: list[dict]) -> list[dict]:
    """Set subscription_added when this invoice already created a subscription."""
    invoice_ids = [str(invoice.get("invoice_id") or "").strip() for invoice in invoices]
    invoice_ids = [invoice_id for invoice_id in invoice_ids if invoice_id]
    added = set()
    if invoice_ids:
        added = set(
            company.subscriptions.filter(zoho_invoice_id__in=invoice_ids)
            .exclude(zoho_invoice_id="")
            .values_list("zoho_invoice_id", flat=True)
        )
    for invoice in invoices:
        invoice["subscription_added"] = str(invoice.get("invoice_id") or "").strip() in added
    return invoices


def build_review_rows(company: CompanyProfile, invoices: list[dict]) -> list[dict]:
    clients = list(company.clients.all())
    products = list(company.products.select_related("vendor").prefetch_related("vendors"))
    existing = set(
        company.subscriptions.exclude(zoho_invoice_id="")
        .exclude(zoho_line_item_id="")
        .values_list("zoho_invoice_id", "zoho_line_item_id")
    )
    rows = []
    for invoice in invoices:
        invoice_id = str(invoice.get("invoice_id") or "").strip()
        invoice_start = _parse_date(invoice.get("date")) or timezone.localdate()
        customer_name = (invoice.get("customer_name") or "").strip()
        gst_no = (invoice.get("gst_no") or "").strip()
        client, client_note = _match_client(clients, customer_name, gst_no)
        invoice_number = (invoice.get("invoice_number") or "").strip()
        for line in invoice.get("line_items") or []:
            line_id = str(line.get("line_item_id") or "").strip()
            item_name = _line_name(line)
            if not invoice_id or not line_id or not item_name:
                continue
            product, product_note = _match_product(products, line)
            vendor = _vendor_for_product(product)
            span = duration_dates(line.get("description") or "")
            if span:
                start, end = span
            else:
                start, end = invoice_start, _add_one_year(invoice_start)
            rows.append(
                {
                    "zoho_invoice_id": invoice_id,
                    "zoho_line_item_id": line_id,
                    "invoice_number": invoice_number,
                    "customer_name": customer_name,
                    "gst_no": gst_no,
                    "zoho_item_name": item_name,
                    "category": (
                        ((product.category or product.asset_type) if product else "") or ""
                    ).strip(),
                    "company_name": ((product.company_name if product else "") or "").strip(),
                    "zoho_sku": (line.get("sku") or "").strip(),
                    "client_id": client.pk if client else None,
                    "product_id": product.pk if product else None,
                    "vendor_id": vendor.pk if vendor else None,
                    "quantity": _quantity(line.get("quantity")),
                    "billing_amount": _money(line.get("item_total")),
                    "start_date": start,
                    "end_date": end,
                    "serial_number": _serial_number(line),
                    "already_imported": (invoice_id, line_id) in existing,
                    "client_note": client_note,
                    "product_note": product_note,
                }
            )
    return rows


def pdf_filename(invoice_number: str, invoice_id: str) -> str:
    raw = (invoice_number or invoice_id or "invoice").strip() or "invoice"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._") or "invoice"
    if not safe.lower().endswith(".pdf"):
        safe = f"{safe}.pdf"
    return safe[:80]


def create_subscriptions(company: CompanyProfile, cleaned_rows: list[dict], pdfs: dict) -> tuple[int, int]:
    """Create one subscription per included line. Returns created and skipped counts."""
    created = 0
    skipped = 0
    with transaction.atomic():
        for row in cleaned_rows:
            invoice_id = row["zoho_invoice_id"]
            line_id = row["zoho_line_item_id"]
            if company.subscriptions.filter(
                zoho_invoice_id=invoice_id,
                zoho_line_item_id=line_id,
            ).exists():
                skipped += 1
                continue
            subscription = ClientSubscription(
                company=company,
                client=row["client"],
                product=row["product"],
                vendor=row["vendor"],
                status=ClientSubscription.Status.ACTIVE,
                start_date=row["start_date"],
                end_date=row["end_date"],
                quantity=row["quantity"],
                billing_amount=row["billing_amount"],
                invoice_number=row["invoice_number"],
                serial_number=row["serial_number"],
                subscription_code=row["subscription_code"],
                zoho_invoice_id=invoice_id,
                zoho_line_item_id=line_id,
            )
            pdf = pdfs.get(invoice_id)
            if pdf:
                subscription.invoice_pdf.save(
                    pdf["name"],
                    ContentFile(pdf["bytes"]),
                    save=False,
                )
            try:
                with transaction.atomic():
                    subscription.save()
            except IntegrityError:
                raise ZohoBooksError(
                    "A subscription id in this import is already used. Change it and save again."
                ) from None
            created += 1
    return created, skipped


def get_document(company: CompanyProfile, organization_id: str, kind: str, zoho_id: str) -> dict:
    if kind == ZohoBooksSync.Kind.BILL:
        return get_bill(company, organization_id, zoho_id)
    return get_invoice(company, organization_id, zoho_id)


def _line_models(document: ZohoBooksDocument, payload: dict) -> list[ZohoBooksDocumentLine]:
    rows = []
    seen: set[str] = set()
    for index, line in enumerate(payload.get("line_items") or []):
        name = _line_name(line)
        if not name:
            continue
        item_id = str(line.get("item_id") or "").strip() or name.casefold()
        line_id = str(line.get("line_item_id") or "").strip() or f"{item_id}-{index}"
        line_id = line_id[:64]
        if line_id in seen:
            line_id = f"{line_id}-{index}"[:64]
        seen.add(line_id)
        rows.append(
            ZohoBooksDocumentLine(
                document=document,
                line_id=line_id,
                item_id=item_id[:64],
                name=name[:255],
                description=(line.get("description") or "")[:4000],
                sku=(line.get("sku") or "")[:128],
                quantity=_quantity(line.get("quantity")),
                amount=_money(line.get("item_total")),
                serial_number=_serial_number(line),
                sort_order=index,
            )
        )
    return rows


def _save_document(
    company: CompanyProfile,
    organization_id: str,
    kind: str,
    summary: dict,
    detail: dict | None,
    *,
    modified_raw: str,
) -> None:
    source = detail or summary
    zoho_id = _document_id(kind, source) or _document_id(kind, summary)
    if not zoho_id:
        return
    defaults = {
        "number": _document_number(kind, source) or _document_number(kind, summary),
        "document_date": _parse_date(source.get("date") or summary.get("date") or ""),
        "due_date": _parse_date(source.get("due_date") or summary.get("due_date") or ""),
        "party_name": _document_party(kind, source) or _document_party(kind, summary),
        "gst_no": (source.get("gst_no") or summary.get("gst_no") or "")[:32],
        "status": (source.get("status") or summary.get("status") or "")[:32],
        "currency_code": (source.get("currency_code") or summary.get("currency_code") or "")[:8],
        "total": _money(source.get("total") if source.get("total") not in (None, "") else summary.get("total")),
        "balance": _optional_money(source, "balance"),
        "last_modified_raw": modified_raw[:40],
    }
    with transaction.atomic():
        document, _created = ZohoBooksDocument.objects.update_or_create(
            company=company,
            organization_id=organization_id,
            kind=kind,
            zoho_id=zoho_id,
            defaults=defaults,
        )
        if detail is None:
            return
        document.lines.all().delete()
        ZohoBooksDocumentLine.objects.bulk_create(_line_models(document, detail))


def _retry_incomplete(company, organization_id: str, kind: str) -> int:
    """Fetch product lines that failed on an earlier sync. Returns how many failed again."""
    pending = list(
        ZohoBooksDocument.objects.filter(
            company=company,
            organization_id=organization_id,
            kind=kind,
            last_modified_raw="",
        ).order_by("id")[:5]
    )
    failed = 0
    for document in pending:
        try:
            detail = get_document(company, organization_id, kind, document.zoho_id)
        except ZohoBooksError:
            logger.warning(
                "Zoho document retry failed company_id=%s kind=%s zoho_id=%s",
                company.pk,
                kind,
                document.zoho_id,
            )
            document.last_modified_raw = "error"
            document.save(update_fields=["last_modified_raw", "synced_at"])
            failed += 1
            continue
        raw = str(detail.get("last_modified_time") or "saved")
        _save_document(
            company,
            organization_id,
            kind,
            detail,
            detail,
            modified_raw=raw,
        )
    return failed


def cancel_zoho_sync(company: CompanyProfile, organization_id: str, kind: str) -> None:
    """Stop an in-progress copy. The next Sync starts clean."""
    ZohoBooksSync.objects.filter(
        company=company,
        organization_id=organization_id,
        kind=kind,
    ).exclude(mode=ZohoBooksSync.Mode.IDLE).update(
        mode=ZohoBooksSync.Mode.IDLE,
        next_page=1,
    )


def release_stale_zoho_sync(
    company: CompanyProfile,
    organization_id: str,
    kind: str,
    *,
    max_age_seconds: int = 180,
) -> None:
    """A refresh abandons the browser chain. Clear a sync that nobody is continuing."""
    cutoff = timezone.now() - timedelta(seconds=max_age_seconds)
    ZohoBooksSync.objects.filter(
        company=company,
        organization_id=organization_id,
        kind=kind,
        updated_at__lt=cutoff,
    ).exclude(mode=ZohoBooksSync.Mode.IDLE).update(
        mode=ZohoBooksSync.Mode.IDLE,
        next_page=1,
    )


def sync_zoho_documents(
    company: CompanyProfile,
    organization_id: str,
    kind: str,
    *,
    start: bool,
) -> dict:
    """Save one Zoho page. The first completed sync is full; later syncs are changes only."""
    if kind not in {ZohoBooksSync.Kind.INVOICE, ZohoBooksSync.Kind.BILL}:
        raise ZohoBooksError("Choose sales invoices or purchase bills.")
    with transaction.atomic():
        state, _created = ZohoBooksSync.objects.select_for_update().get_or_create(
            company=company,
            organization_id=organization_id,
            kind=kind,
            defaults={"mode": ZohoBooksSync.Mode.IDLE, "next_page": 1},
        )
        if start and state.mode == ZohoBooksSync.Mode.IDLE:
            ZohoBooksDocument.objects.filter(
                company=company,
                organization_id=organization_id,
                kind=kind,
                last_modified_raw="error",
            ).update(last_modified_raw="")
            state.mode = (
                ZohoBooksSync.Mode.INCREMENTAL
                if state.last_completed_at
                else ZohoBooksSync.Mode.FULL
            )
            state.next_page = 1
            state.save(update_fields=["mode", "next_page", "updated_at"])
        mode = state.mode
        page = max(state.next_page or 1, 1)
        modified_after = state.modified_after if mode == ZohoBooksSync.Mode.INCREMENTAL else None

    if mode == ZohoBooksSync.Mode.IDLE:
        return _sync_status(company, organization_id, kind, page=1, has_more=False, failed=0)

    failed = _retry_incomplete(company, organization_id, kind)
    summaries, page_context = list_documents_page(
        company,
        organization_id,
        kind,
        page=page,
        modified_after=modified_after,
    )
    ids = [_document_id(kind, summary) for summary in summaries]
    ids = [zoho_id for zoho_id in ids if zoho_id]
    known = {
        document.zoho_id: document.last_modified_raw
        for document in ZohoBooksDocument.objects.filter(
            company=company,
            organization_id=organization_id,
            kind=kind,
            zoho_id__in=ids,
        )
    }

    def fetch_one(summary: dict):
        zoho_id = _document_id(kind, summary)
        raw = str(summary.get("last_modified_time") or "").strip()
        if zoho_id and raw and known.get(zoho_id) == raw:
            return None
        detail = None
        saved_raw = raw
        if zoho_id:
            try:
                detail = get_document(company, organization_id, kind, zoho_id)
                saved_raw = str(detail.get("last_modified_time") or raw or "saved")
            except ZohoBooksError:
                logger.warning(
                    "Zoho document detail failed company_id=%s kind=%s zoho_id=%s",
                    company.pk,
                    kind,
                    zoho_id,
                )
                saved_raw = ""
        return summary, detail, saved_raw

    fetched = []
    if summaries:
        workers = min(6, len(summaries))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fetch_one, summary) for summary in summaries]
            for future in as_completed(futures):
                fetched.append(future.result())
    for item in fetched:
        if item is None:
            continue
        summary, detail, saved_raw = item
        _save_document(
            company,
            organization_id,
            kind,
            summary,
            detail,
            modified_raw=saved_raw,
        )

    has_more = bool(page_context.get("has_more_page"))
    with transaction.atomic():
        state = ZohoBooksSync.objects.select_for_update().get(
            company=company,
            organization_id=organization_id,
            kind=kind,
        )
        if state.mode == ZohoBooksSync.Mode.IDLE:
            # Stop was clicked, or the page was left and the sync was cleared.
            has_more = False
        elif state.mode == mode and state.next_page == page:
            if has_more and summaries:
                state.next_page = page + 1
            else:
                state.mode = ZohoBooksSync.Mode.IDLE
                state.next_page = 1
                state.last_completed_at = timezone.now()
                state.modified_after = timezone.now() - timedelta(minutes=2)
            state.save(
                update_fields=[
                    "mode",
                    "next_page",
                    "last_completed_at",
                    "modified_after",
                    "updated_at",
                ]
            )
            has_more = state.mode != ZohoBooksSync.Mode.IDLE
        else:
            has_more = False
    return _sync_status(
        company,
        organization_id,
        kind,
        page=page,
        has_more=has_more,
        failed=failed,
    )


def _sync_status(company, organization_id: str, kind: str, *, page: int, has_more: bool, failed: int) -> dict:
    sync = ZohoBooksSync.objects.filter(
        company=company,
        organization_id=organization_id,
        kind=kind,
    ).first()
    stored = ZohoBooksDocument.objects.filter(
        company=company,
        organization_id=organization_id,
        kind=kind,
    ).count()
    return {
        "page": page,
        "has_more": has_more,
        "stored": stored,
        "failed": failed,
        "synced_at": _synced_label(sync),
    }


def _synced_label(sync: ZohoBooksSync | None) -> str:
    if sync is None or sync.last_completed_at is None:
        return ""
    return timezone.localtime(sync.last_completed_at).strftime("%d %b %Y, %H:%M")


def stored_invoice_dict(company: CompanyProfile, organization_id: str, invoice_id: str) -> dict | None:
    document = (
        ZohoBooksDocument.objects.filter(
            company=company,
            organization_id=organization_id,
            kind=ZohoBooksSync.Kind.INVOICE,
            zoho_id=str(invoice_id),
        )
        .prefetch_related("lines")
        .first()
    )
    if document is None:
        return None
    line_items = []
    for line in document.lines.all():
        line_items.append(
            {
                "line_item_id": line.line_id,
                "item_id": line.item_id,
                "name": line.name,
                "description": line.description,
                "sku": line.sku,
                "quantity": line.quantity,
                "item_total": line.amount,
                "serial_numbers": [line.serial_number] if line.serial_number else [],
            }
        )
    return {
        "invoice_id": document.zoho_id,
        "invoice_number": document.number,
        "date": document.document_date.isoformat() if document.document_date else "",
        "customer_name": document.party_name,
        "gst_no": document.gst_no,
        "status": document.status,
        "currency_code": document.currency_code,
        "total": str(document.total),
        "line_items": line_items,
    }


def list_stored_documents(
    company: CompanyProfile,
    organization_id: str,
    kind: str,
    *,
    page: int = 1,
    year: str = "",
    month: str = "",
    query: str = "",
    item_ids: list[str] | None = None,
) -> dict:
    base = ZohoBooksDocument.objects.filter(
        company=company,
        organization_id=organization_id,
        kind=kind,
    )
    if year:
        period = invoice_period_range(year, month)
        if period:
            base = base.filter(document_date__gte=period[0], document_date__lte=period[1])
    elif month.isdigit() and 1 <= int(month) <= 12:
        base = base.filter(document_date__month=int(month))
    query = (query or "").strip()
    if query:
        base = base.filter(number__icontains=query)
    products = []
    seen_items: set[str] = set()
    stamp = (
        base.order_by("-id").values_list("id", flat=True).first()
        or 0
    )
    product_key = (
        f"zoho-doc-products:{company.pk}:{organization_id}:{kind}:"
        f"{stamp}:{year}:{month}:{query.casefold()}"
    )
    cached_products = cache.get(product_key)
    if cached_products is not None:
        products = cached_products
        product_rows = []
    else:
        line_qs = ZohoBooksDocumentLine.objects.filter(
            document__company=company,
            document__organization_id=organization_id,
            document__kind=kind,
        ).exclude(name="")
        if year:
            period = invoice_period_range(year, month)
            if period:
                line_qs = line_qs.filter(
                    document__document_date__gte=period[0],
                    document__document_date__lte=period[1],
                )
        elif month.isdigit() and 1 <= int(month) <= 12:
            line_qs = line_qs.filter(document__document_date__month=int(month))
        if query:
            line_qs = line_qs.filter(document__number__icontains=query)
        product_rows = (
            line_qs.values("item_id", "name", "sku")
            .distinct()
            .order_by("name", "item_id")
        )
    for row in product_rows:
        item_id = (row["item_id"] or "").strip() or (row["name"] or "").casefold()
        if not item_id or item_id in seen_items:
            continue
        seen_items.add(item_id)
        products.append(
            {
                "item_id": item_id,
                "name": row["name"],
                "sku": (row["sku"] or "").strip(),
            }
        )
    if cached_products is None:
        cache.set(product_key, products, 120)
    selected = []
    seen_selected: set[str] = set()
    for item_id in item_ids or []:
        item_id = str(item_id).strip()
        if item_id and item_id not in seen_selected:
            seen_selected.add(item_id)
            selected.append(item_id)
    documents = base
    if selected:
        documents = documents.filter(lines__item_id__in=selected).distinct()
    total = documents.count()
    page = max(page, 1)
    start = (page - 1) * STORED_PAGE_SIZE
    rows = []
    page_rows = documents.prefetch_related("lines").order_by("-document_date", "-id")[
        start : start + STORED_PAGE_SIZE
    ]
    for document in page_rows:
        items = []
        for line in document.lines.all():
            items.append(
                {
                    "item_id": line.item_id or line.name.casefold(),
                    "name": line.name,
                    "sku": line.sku,
                    "quantity": line.quantity,
                    "amount": str(line.amount),
                }
            )
        rows.append(
            {
                "zoho_id": document.zoho_id,
                "invoice_id": document.zoho_id,
                "number": document.number,
                "invoice_number": document.number,
                "date": document.document_date.isoformat() if document.document_date else "",
                "due_date": document.due_date.isoformat() if document.due_date else "",
                "party_name": document.party_name,
                "customer_name": document.party_name,
                "status": document.status,
                "currency_code": document.currency_code,
                "total": str(document.total),
                "balance": "" if document.balance is None else str(document.balance),
                "items_loaded": document.last_modified_raw not in ("", "error"),
                "items": items,
            }
        )
    if kind == ZohoBooksSync.Kind.INVOICE:
        mark_imported_invoices(company, rows)
    sync = ZohoBooksSync.objects.filter(
        company=company,
        organization_id=organization_id,
        kind=kind,
    ).first()
    saved_any = ZohoBooksDocument.objects.filter(
        company=company,
        organization_id=organization_id,
        kind=kind,
    ).exists()
    ever_synced = saved_any
    return {
        "page": page,
        "has_more_page": start + STORED_PAGE_SIZE < total,
        "count": total,
        "documents": rows,
        "products": products,
        "synced_at": _synced_label(sync),
        "ever_synced": ever_synced,
        "year": year,
        "month": month,
    }
