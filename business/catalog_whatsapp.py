"""Catalog list builders for WhatsApp interactive messages."""

from __future__ import annotations

from accounts.models import CompanyProfile

from .catalog import (
    CatalogCategory,
    CatalogMasterCategory,
    CatalogProduct,
    CatalogSubcategory,
)

MASTER_REPLY_PREFIX = "master_"


def master_list_rows() -> list[dict[str, str]]:
    return [
        {
            "id": f"{MASTER_REPLY_PREFIX}{CatalogMasterCategory.HARDWARE}",
            "title": "Hardware",
            "description": "Laptops, desktops, devices",
        },
        {
            "id": f"{MASTER_REPLY_PREFIX}{CatalogMasterCategory.SOFTWARE}",
            "title": "Software",
            "description": "Licenses and subscriptions",
        },
        {
            "id": f"{MASTER_REPLY_PREFIX}{CatalogMasterCategory.OTHERS}",
            "title": "Others",
            "description": "Accessories and more",
        },
    ]


def parse_master_reply_id(reply_id: str) -> str | None:
    rid = (reply_id or "").strip().lower()
    if not rid.startswith(MASTER_REPLY_PREFIX):
        return None
    key = rid[len(MASTER_REPLY_PREFIX) :]
    if key in CatalogMasterCategory.values:
        return key
    return None


def categories_for_master(
    company: CompanyProfile | None, master_key: str
) -> list[CatalogCategory]:
    if company is None:
        return []
    return list(
        CatalogCategory.objects.filter(
            company=company,
            master_category=master_key,
            is_active=True,
        ).order_by("sort_order", "name")[:10]
    )


def category_list_rows(categories: list[CatalogCategory]) -> list[dict[str, str]]:
    return [
        {
            "id": f"ccat_{c.pk}",
            "title": c.name[:24],
            "description": c.get_master_category_display()[:72],
        }
        for c in categories
    ]


def parse_category_reply_id(reply_id: str) -> int | None:
    rid = (reply_id or "").strip().lower()
    if not rid.startswith("ccat_"):
        return None
    try:
        return int(rid.split("_", 1)[1])
    except (IndexError, ValueError):
        return None


def subcategories_for_category(category_id: int) -> list[CatalogSubcategory]:
    return list(
        CatalogSubcategory.objects.filter(
            category_id=category_id,
            is_active=True,
        )
        .select_related("category")
        .order_by("sort_order", "name")[:10]
    )


def subcategory_list_rows(subcategories: list[CatalogSubcategory]) -> list[dict[str, str]]:
    return [
        {
            "id": f"csub_{s.pk}",
            "title": s.name[:24],
            "description": (s.description or "")[:72],
        }
        for s in subcategories
    ]


def parse_subcategory_reply_id(reply_id: str) -> int | None:
    rid = (reply_id or "").strip().lower()
    if not rid.startswith("csub_"):
        return None
    try:
        return int(rid.split("_", 1)[1])
    except (IndexError, ValueError):
        return None


def products_for_subcategory(subcategory_id: int) -> list[CatalogProduct]:
    return list(
        CatalogProduct.objects.filter(
            subcategory_id=subcategory_id,
            is_active=True,
            is_whatsapp_visible=True,
        )
        .select_related("subcategory__category")
        .order_by("sort_order", "brand", "product_name")[:10]
    )


def product_list_rows(products: list[CatalogProduct]) -> list[dict[str, str]]:
    return [
        {
            "id": f"cprod_{p.pk}",
            "title": p.whatsapp_list_title(),
            "description": p.whatsapp_list_description(),
        }
        for p in products
    ]


def parse_product_reply_id(reply_id: str) -> int | None:
    rid = (reply_id or "").strip().lower()
    if not rid.startswith("cprod_"):
        return None
    try:
        return int(rid.split("_", 1)[1])
    except (IndexError, ValueError):
        return None
