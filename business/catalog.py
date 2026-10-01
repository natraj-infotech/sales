"""WhatsApp / customer-facing procurement catalog (master → category → subcategory → product)."""

from __future__ import annotations

from decimal import Decimal

from django.db import models

from accounts.models import CompanyProfile


class CatalogMasterCategory(models.TextChoices):
    HARDWARE = "hardware", "Hardware"
    SOFTWARE = "software", "Software"
    OTHERS = "others", "Others"


class CatalogCategory(models.Model):
    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="catalog_categories",
    )
    master_category = models.CharField(
        max_length=16,
        choices=CatalogMasterCategory.choices,
        db_index=True,
    )
    name = models.CharField(max_length=128)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["master_category", "sort_order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "master_category", "name"],
                name="unique_catalog_category_per_master",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_master_category_display()} / {self.name}"


class CatalogSubcategory(models.Model):
    category = models.ForeignKey(
        CatalogCategory,
        on_delete=models.CASCADE,
        related_name="subcategories",
    )
    name = models.CharField(max_length=128)
    description = models.CharField(max_length=255, blank=True)
    brochure_pdf = models.FileField(
        upload_to="catalog/brochures/%Y/%m/",
        blank=True,
        null=True,
    )
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "Catalog subcategories"
        constraints = [
            models.UniqueConstraint(
                fields=["category", "name"],
                name="unique_catalog_subcategory_name",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.category.name} / {self.name}"

    @property
    def company(self) -> CompanyProfile:
        return self.category.company


class CatalogProduct(models.Model):
    subcategory = models.ForeignKey(
        CatalogSubcategory,
        on_delete=models.CASCADE,
        related_name="products",
    )
    brand = models.CharField(max_length=128)
    product_name = models.CharField(max_length=255)
    model_number = models.CharField(max_length=128, blank=True)
    specs = models.TextField(blank=True, help_text="Optional CPU/RAM/storage details.")
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Leave blank for price on request.",
    )
    datasheet_pdf = models.FileField(
        upload_to="catalog/datasheets/%Y/%m/",
        blank=True,
        null=True,
    )
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    is_whatsapp_visible = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "brand", "product_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["subcategory", "brand", "product_name", "model_number"],
                name="unique_catalog_product_sku",
            ),
        ]

    def __str__(self) -> str:
        return self.display_label()

    def display_label(self) -> str:
        parts = [self.brand.strip(), self.product_name.strip()]
        if (self.model_number or "").strip():
            parts.append(self.model_number.strip())
        return " ".join(p for p in parts if p)

    def whatsapp_list_title(self, max_len: int = 24) -> str:
        label = self.display_label()
        if len(label) <= max_len:
            return label
        return label[: max_len - 1] + "…"

    def whatsapp_list_description(self, max_len: int = 72) -> str:
        if self.unit_price is not None and self.unit_price > 0:
            text = f"₹{self.unit_price:,.2f}"
        else:
            text = "Price on request"
        if self.specs:
            extra = self.specs.strip().replace("\n", " ")[:40]
            text = f"{text} · {extra}" if extra else text
        return text[:max_len]

    def priced(self) -> bool:
        return self.unit_price is not None and self.unit_price > 0

    @property
    def company(self) -> CompanyProfile:
        return self.subcategory.company

    @property
    def master_category(self) -> str:
        return self.subcategory.category.master_category

    @property
    def category_name(self) -> str:
        return self.subcategory.category.name

    @property
    def subcategory_name(self) -> str:
        return self.subcategory.name


class CustomerQuotation(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        PENDING_PRICING = "pending_pricing", "Pending pricing"

    company = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="customer_quotations",
    )
    lead = models.ForeignKey(
        "business.Lead",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="customer_quotations",
    )
    customer_company_name = models.CharField(max_length=255, blank=True)
    whatsapp_wa_id = models.CharField(max_length=32, blank=True, db_index=True)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    has_unpriced_lines = models.BooleanField(default=False)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    pdf = models.FileField(upload_to="quotations/%Y/%m/", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Quote #{self.pk} ({self.get_status_display()})"


class CustomerQuotationLine(models.Model):
    quotation = models.ForeignKey(
        CustomerQuotation,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    catalog_product = models.ForeignKey(
        CatalogProduct,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="quotation_lines",
    )
    master_category = models.CharField(max_length=16, blank=True)
    category_name = models.CharField(max_length=128, blank=True)
    subcategory_name = models.CharField(max_length=128, blank=True)
    display_name = models.CharField(max_length=512)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    line_total = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )

    class Meta:
        ordering = ["id"]
