"""Forms for catalog configuration."""

from django import forms

from .catalog import CatalogCategory, CatalogProduct, CatalogSubcategory


class CatalogCategoryForm(forms.ModelForm):
    class Meta:
        model = CatalogCategory
        fields = ["master_category", "name", "sort_order", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-input", "placeholder": "e.g. Laptop"}),
            "sort_order": forms.NumberInput(attrs={"class": "form-input", "style": "max-width:6rem"}),
        }


class CatalogSubcategoryForm(forms.ModelForm):
    class Meta:
        model = CatalogSubcategory
        fields = ["name", "description", "brochure_pdf", "sort_order", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-input", "placeholder": "e.g. Intel i5"}),
            "description": forms.TextInput(attrs={"class": "form-input"}),
            "sort_order": forms.NumberInput(attrs={"class": "form-input", "style": "max-width:6rem"}),
        }


class CatalogProductForm(forms.ModelForm):
    class Meta:
        model = CatalogProduct
        fields = [
            "brand",
            "product_name",
            "model_number",
            "specs",
            "unit_price",
            "datasheet_pdf",
            "sort_order",
            "is_active",
            "is_whatsapp_visible",
        ]
        widgets = {
            "brand": forms.TextInput(attrs={"class": "form-input", "placeholder": "Dell"}),
            "product_name": forms.TextInput(attrs={"class": "form-input", "placeholder": "Latitude"}),
            "model_number": forms.TextInput(attrs={"class": "form-input", "placeholder": "5540"}),
            "specs": forms.Textarea(attrs={"class": "form-input", "rows": 3}),
            "unit_price": forms.NumberInput(attrs={"class": "form-input", "step": "0.01"}),
            "sort_order": forms.NumberInput(attrs={"class": "form-input", "style": "max-width:6rem"}),
        }
