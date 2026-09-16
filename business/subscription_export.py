"""Build a column- and company-filtered Excel export of client subscriptions."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from io import BytesIO

from django.db.models import Q, QuerySet
from openpyxl import Workbook

from business.models import ClientSubscription

NO_COMPANY_VALUE = "__none__"
NO_COMPANY_LABEL = "(No company)"

EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("serial_no", "Serial No"),
    ("client", "Client"),
    ("category", "Category"),
    ("company", "Company"),
    ("product", "Product"),
    ("quantity", "Quantity"),
    ("start_date", "Start Date"),
    ("end_date", "End Date"),
    ("status", "Status"),
    ("vendor", "Vendor"),
    ("price", "Price"),
    ("invoice", "Invoice #"),
    ("subscription_id", "Subscription ID"),
    ("description", "Description"),
)

EXPORT_COLUMN_LABELS: dict[str, str] = dict(EXPORT_COLUMNS)
EXPORT_COLUMN_KEYS: tuple[str, ...] = tuple(key for key, _label in EXPORT_COLUMNS)

DEFAULT_EXPORT_COLUMNS: tuple[str, ...] = (
    "client",
    "category",
    "product",
    "company",
    "vendor",
    "quantity",
    "start_date",
    "end_date",
    "price",
    "serial_no",
    "subscription_id",
)


def normalize_column_keys(raw_keys: Iterable[str]) -> list[str]:
    allowed = set(EXPORT_COLUMN_KEYS)
    seen: set[str] = set()
    keys: list[str] = []
    for raw in raw_keys:
        key = (raw or "").strip()
        if key in allowed and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def distinct_product_company_names(
    qs: QuerySet[ClientSubscription],
) -> tuple[list[str], bool]:
    names: list[str] = []
    seen: set[str] = set()
    has_blank = False
    for raw in qs.values_list("product__company_name", flat=True).distinct():
        name = (raw or "").strip()
        if not name:
            has_blank = True
            continue
        key = name.lower()
        if key not in seen:
            seen.add(key)
            names.append(name)
    names.sort(key=str.lower)
    return names, has_blank


def apply_company_selection(
    qs: QuerySet[ClientSubscription], selected: Sequence[str]
) -> QuerySet[ClientSubscription]:
    values = [(item or "").strip() for item in selected if (item or "").strip()]
    if not values:
        return qs.none()
    include_blank = NO_COMPANY_VALUE in values
    names = [item for item in values if item != NO_COMPANY_VALUE]
    query = Q()
    if names:
        name_query = Q()
        for name in names:
            name_query |= Q(product__company_name__iexact=name)
        query |= name_query
    if include_blank:
        query |= Q(product__company_name="") | Q(product__company_name__isnull=True)
    return qs.filter(query)


def _cell_value(subscription: ClientSubscription, key: str):
    product = subscription.product
    vendor = subscription.vendor
    if key == "serial_no":
        return (subscription.serial_number or "").strip()
    if key == "client":
        return subscription.client.name if subscription.client_id else ""
    if key == "category":
        return (product.category or "").strip() if product else ""
    if key == "company":
        return (product.company_name or "").strip() if product else ""
    if key == "product":
        return product.name if product else ""
    if key == "quantity":
        return subscription.quantity
    if key == "start_date":
        return subscription.start_date
    if key == "end_date":
        return subscription.end_date
    if key == "status":
        return subscription.get_status_display()
    if key == "vendor":
        return vendor.name if vendor else ""
    if key == "price":
        return subscription.billing_amount
    if key == "invoice":
        return (subscription.invoice_number or "").strip()
    if key == "subscription_id":
        return subscription.subscription_list_id
    if key == "description":
        return (subscription.notes or "").strip()
    return ""


def build_subscription_export_bytes(
    subscriptions: Iterable[ClientSubscription],
    column_keys: Sequence[str],
) -> bytes:
    keys = normalize_column_keys(column_keys)
    if not keys:
        raise ValueError("Select at least one column to export.")

    wb = Workbook()
    ws = wb.active
    ws.title = "Subscriptions"
    ws.append([EXPORT_COLUMN_LABELS[key] for key in keys])
    for subscription in subscriptions:
        ws.append([_cell_value(subscription, key) for key in keys])

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
