from django import forms
from django.db.models import Q

from .models import (
    Vendor,
    Client,
    CaseLockInbox,
    CaseLockSubmission,
    ClientSubscription,
    Product,
    SupportTicket,
    SupportTicketComment,
)


class SupportTicketForm(forms.ModelForm):
    class Meta:
        model = SupportTicket
        fields = ("subject", "description", "department", "priority")


class SupportTicketUpdateForm(forms.ModelForm):
    class Meta:
        model = SupportTicket
        fields = ("status", "assignee", "remarks")

    def __init__(self, *args, assignee_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if assignee_queryset is not None:
            self.fields["assignee"].queryset = assignee_queryset
        self.fields["assignee"].required = False


class SupportTicketCommentForm(forms.ModelForm):
    class Meta:
        model = SupportTicketComment
        fields = ("body", "internal_note")


class VendorForm(forms.ModelForm):
    class Meta:
        model = Vendor
        fields = (
            "name",
            "gst_number",
            "email",
            "email_2",
            "email_3",
            "phone",
            "phone_2",
            "phone_3",
            "website",
            "notes",
        )


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ("name", "gst_number", "email", "phone", "notes")
        widgets = {"notes": forms.HiddenInput()}


class ProductForm(forms.ModelForm):
    category = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Select Category"),
            ("Hardware", "Hardware"),
            ("Software", "Software"),
            ("Service", "Service"),
        ],
    )
    asset_type = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Select Type"),
            ("Laptop", "Laptop"),
            ("CCTV", "CCTV"),
            ("Desktop", "Desktop"),
            ("Server", "Server"),
            ("Network Equipment", "Network Equipment"),
            ("Other", "Other (Enter Below)"),
        ],
    )
    components_count = forms.IntegerField(
        required=False,
        min_value=0,
        initial=0,
        label="Number of Components",
        help_text="Enter 0 if the product has no separate components",
    )
    asset_type_custom = forms.CharField(
        required=False,
        label="Custom Type",
        widget=forms.TextInput(attrs={"placeholder": "Enter custom product type"}),
    )

    class Meta:
        model = Product
        fields = (
            "name",
            "category",
            "asset_type",
            "company_name",
            "tax",
            "depreciation",
            "hsn_code",
            "company_phone",
            "company_email",
            "components",
            "notes",
            "vendor",
            "sku",
            "unit_price",
            "description",
        )

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sku"].label = "Company Part Number"
        self.fields["hsn_code"].label = "HSN Code  (optional)"
        self.fields["hsn_code"].widget.attrs.setdefault("placeholder", "Enter if applicable")
        self.fields["tax"].label = "Tax Percentage (%)"
        self.fields["depreciation"].label = "Depreciation Percentage (%)"
        self.fields["tax"].widget.attrs.setdefault("placeholder", "Enter tax %")
        self.fields["depreciation"].widget.attrs.setdefault("placeholder", "Enter depreciation %")
        self.fields["vendor"].required = False
        self.fields["unit_price"].required = False
        self.fields["description"].required = False
        self.fields["unit_price"].initial = self.fields["unit_price"].initial or 0
        self.fields["notes"].label = "Product Description / Notes"
        self.fields["notes"].help_text = "Optional - Add any additional information about this product"
        self.fields["notes"].widget.attrs.setdefault(
            "placeholder",
            "Enter detailed product description, specifications, or any additional notes...",
        )
        self.fields["notes"].widget.attrs.setdefault("rows", 2)
        self.fields["components"].widget = forms.HiddenInput()
        self.component_items: list[str] = []
        known_types = {v for v, _ in self.fields["asset_type"].choices if v and v != "Other"}
        current_type = (self.instance.asset_type or "").strip() if self.instance.pk else ""
        if current_type and current_type not in known_types:
            self.initial["asset_type"] = "Other"
            self.initial["asset_type_custom"] = current_type
        if self.is_bound:
            raw_count = (self.data.get("components_count") or "").strip()
            try:
                count = max(0, int(raw_count))
            except ValueError:
                count = 0
            self.component_items = [
                (self.data.get(f"component_item_{idx}") or "").strip()
                for idx in range(1, count + 1)
            ]
        else:
            self.component_items = [
                line.strip()
                for line in (self.instance.components or "").splitlines()
                if line.strip()
            ]
            self.initial["components_count"] = len(self.component_items)
        if company is not None:
            self.fields["vendor"].queryset = company.vendors.all().order_by("name")

    def clean(self):
        cleaned = super().clean()
        asset_type = (cleaned.get("asset_type") or "").strip()
        custom = (cleaned.get("asset_type_custom") or "").strip()
        if asset_type == "Other":
            if not custom:
                self.add_error("asset_type_custom", "Enter custom product type.")
            else:
                cleaned["asset_type"] = custom
        else:
            cleaned["asset_type_custom"] = ""
        count = cleaned.get("components_count") or 0
        component_items: list[str] = []
        for idx in range(1, count + 1):
            item = (self.data.get(f"component_item_{idx}") or "").strip()
            if not item:
                self.add_error("components_count", f"Enter a name for component {idx}.")
                continue
            component_items.append(item)
        cleaned["components"] = "\n".join(component_items)
        for percent_field in ("tax", "depreciation"):
            raw = (cleaned.get(percent_field) or "").strip()
            if raw and not raw.endswith("%"):
                cleaned[percent_field] = f"{raw}%"
        if cleaned.get("unit_price") in (None, ""):
            cleaned["unit_price"] = 0
        return cleaned


def _subscription_product_queryset(company, data=None, asset_type_prefilter=None, instance=None):
    pq = company.products.all()
    if asset_type_prefilter:
        pq = pq.filter(asset_type__iexact=asset_type_prefilter)
    if data is not None:
        cat = (data.get("category_filter") or "").strip()
        cn = (data.get("company_name_filter") or "").strip()
        if cat:
            pq = pq.filter(Q(category__iexact=cat) | Q(asset_type__iexact=cat))
        if cn:
            pq = pq.filter(company_name=cn)
    if instance and instance.pk and instance.product_id:
        pq = (pq | company.products.filter(pk=instance.product_id)).distinct()
    return pq


class ClientSubscriptionForm(forms.ModelForm):
    category_filter = forms.ChoiceField(
        label="Category",
        required=False,
    )
    company_name_filter = forms.ChoiceField(
        label="Company name",
        required=False,
    )

    class Meta:
        model = ClientSubscription
        fields = (
            "client",
            "product",
            "quantity",
            "start_date",
            "end_date",
            "vendor",
            "billing_amount",
            "invoice_number",
            "serial_number",
            "subscription_code",
            "status",
            "notes",
        )
        widgets = {
            "status": forms.HiddenInput(),
            "notes": forms.HiddenInput(),
            "start_date": forms.DateInput(
                attrs={"type": "date", "placeholder": "dd-mm-yyyy"},
                format="%Y-%m-%d",
            ),
            "end_date": forms.DateInput(
                attrs={"type": "date", "placeholder": "dd-mm-yyyy"},
                format="%Y-%m-%d",
            ),
        }

    def __init__(self, *args, company=None, asset_type_prefilter=None, **kwargs):
        self.company = company
        self.asset_type_prefilter = (asset_type_prefilter or "").strip() or None
        super().__init__(*args, **kwargs)

        if not self.instance.pk:
            self.fields["status"].initial = ClientSubscription.Status.ACTIVE

        self.fields["billing_amount"].label = "Price"
        self.fields["invoice_number"].label = "Invoice number"
        self.fields["subscription_code"].label = "Subscription id"

        cats = set()
        if company:
            for p in company.products.all():
                if p.category:
                    cats.add(p.category)
                if p.asset_type:
                    cats.add(p.asset_type)
        cat_list = sorted(cats, key=str.lower)
        if self.asset_type_prefilter:
            self.fields.pop("category_filter", None)
        else:
            self.fields["category_filter"].choices = [("", "---------")] + [
                (c, c) for c in cat_list
            ]
            if self.instance.pk and self.instance.product_id:
                p = self.instance.product
                init = p.category or p.asset_type or ""
                if init:
                    self.fields["category_filter"].initial = init

        companies = []
        if company:
            companies = list(
                company.products.exclude(company_name="")
                .values_list("company_name", flat=True)
                .distinct()
                .order_by("company_name")
            )
        self.fields["company_name_filter"].choices = [("", "Select company")] + [
            (c, c) for c in companies
        ]
        if self.instance.pk and self.instance.product_id:
            cn = (self.instance.product.company_name or "").strip()
            if cn and cn not in companies:
                self.fields["company_name_filter"].choices.append((cn, cn))
            self.fields["company_name_filter"].initial = cn

        data = self.data if self.data else None
        pq = _subscription_product_queryset(
            company,
            data=data,
            asset_type_prefilter=self.asset_type_prefilter,
            instance=self.instance if self.instance.pk else None,
        )
        self.fields["product"].queryset = pq.order_by("name")
        self.fields["product"].empty_label = "Select product"

        if company:
            self.fields["client"].queryset = company.clients.all()
            self.fields["vendor"].queryset = company.vendors.all()
            self.fields["client"].empty_label = "---------"
            self.fields["vendor"].empty_label = "---------"

    def save(self, commit=True):
        obj = super().save(commit=False)
        if commit:
            obj.save()
        return obj


class CaseLockInboxForm(forms.ModelForm):
    class Meta:
        model = CaseLockInbox
        fields = (
            "product",
            "mailbox_email",
            "mailbox_app_password",
            "is_active",
            "notes",
        )
        widgets = {
            "mailbox_app_password": forms.PasswordInput(render_value=True),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["product"].queryset = company.products.all().order_by("name")


class CaseLockPublicForm(forms.ModelForm):
    class Meta:
        model = CaseLockSubmission
        fields = (
            "vendor_name",
            "vendor_gst",
            "client_name",
            "client_phone",
            "client_email",
            "client_email_2",
            "client_gst",
            "product_name",
            "product_quantity",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["client_phone"].widget.attrs.setdefault(
            "placeholder",
            "Client phone number",
        )
        self.fields["client_email"].widget.attrs.setdefault(
            "placeholder",
            "Client email",
        )
        self.fields["client_email_2"].label = "Client email 2 (optional)"
        self.fields["client_email_2"].widget.attrs.setdefault(
            "placeholder",
            "Additional client email",
        )
