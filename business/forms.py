from django import forms
from django.forms import BaseFormSet, formset_factory
from django.db.models import Q
from django.db.models.functions import Trim

from .models import (
    Vendor,
    Client,
    CaseLockInbox,
    CaseLockProduct,
    CaseLockSubmission,
    ClientSubscription,
    Product,
    SupportTicket,
    SupportSLASettings,
    TicketPriorityRule,
)


class SupportTicketForm(forms.Form):
    department = forms.ChoiceField(
        choices=SupportTicket.Department.choices,
        initial=SupportTicket.Department.GENERAL,
    )
    priority = forms.ChoiceField(
        choices=SupportTicket.Priority.choices,
        initial=SupportTicket.Priority.MEDIUM,
    )
    subject = forms.CharField(max_length=255)
    description = forms.CharField(widget=forms.Textarea)


class SupportTicketUpdateForm(forms.Form):
    status = forms.ChoiceField(choices=SupportTicket.Status.choices, required=False)
    assignee = forms.ModelChoiceField(queryset=None, required=False)
    remarks = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, assignee_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assignee"].queryset = assignee_queryset or SupportTicket._meta.get_field(
            "assignee"
        ).related_model.objects.none()


class SupportTicketCommentForm(forms.Form):
    body = forms.CharField(widget=forms.Textarea, label="Comment")
    internal_note = forms.BooleanField(
        required=False,
        label="Internal note",
        help_text="Internal notes are not emailed to the submitter.",
    )


class SupportSLASettingsForm(forms.ModelForm):
    class Meta:
        model = SupportSLASettings
        fields = (
            "sla_mode",
            "email_auto_priority",
            "urgent_response_hours",
            "high_response_hours",
            "medium_response_hours",
            "low_response_hours",
            "urgent_resolve_hours",
            "high_resolve_hours",
            "medium_resolve_hours",
            "low_resolve_hours",
        )
        labels = {
            "sla_mode": "SLA mode",
            "email_auto_priority": "Auto-set priority from email subject keywords",
            "urgent_response_hours": "Urgent — first response (hours)",
            "high_response_hours": "High — first response (hours)",
            "medium_response_hours": "Medium — first response (hours)",
            "low_response_hours": "Low — first response (hours)",
            "urgent_resolve_hours": "Urgent — resolve (hours)",
            "high_resolve_hours": "High — resolve (hours)",
            "medium_resolve_hours": "Medium — resolve (hours)",
            "low_resolve_hours": "Low — resolve (hours)",
        }
        widgets = {
            "sla_mode": forms.RadioSelect,
        }


class TicketPriorityRuleForm(forms.ModelForm):
    priority = forms.ChoiceField(
        choices=SupportTicket.Priority.choices,
        required=False,
        label="Priority",
    )
    keywords = forms.CharField(
        required=False,
        label="Keywords (comma-separated)",
        widget=forms.TextInput(
            attrs={"placeholder": "urgent, outage, down", "style": "min-width:16rem;"}
        ),
    )
    sort_order = forms.IntegerField(
        required=False,
        initial=0,
        label="Order",
        widget=forms.NumberInput(attrs={"style": "width:4.5rem;"}),
    )

    class Meta:
        model = TicketPriorityRule
        fields = ("priority", "keywords", "sort_order")


TicketPriorityRuleFormSet = formset_factory(
    TicketPriorityRuleForm,
    extra=1,
    can_delete=True,
)


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
        fields = (
            "name",
            "gst_number",
            "email",
            "email_2",
            "email_3",
            "phone",
            "phone_2",
            "phone_3",
            "notes",
            "renewal_emails_enabled",
            "reminder_template",
        )
        widgets = {
            "notes": forms.HiddenInput(),
            "renewal_emails_enabled": forms.CheckboxInput(),
        }

    def __init__(self, *args, company=None, **kwargs):
        from business.models import ReminderTemplate
        from business.reminder_templates import ensure_default_reminder_template

        self.company = company
        super().__init__(*args, **kwargs)
        for name in (
            "gst_number",
            "email",
            "email_2",
            "email_3",
            "phone",
            "phone_2",
            "phone_3",
        ):
            self.fields[name].required = False
        self.fields["renewal_emails_enabled"].label = "Renewal emails"
        self.fields["renewal_emails_enabled"].required = False
        self.fields["renewal_emails_enabled"].help_text = (
            "When on, this client receives expiry / renewal reminder emails."
        )
        if company is not None:
            ensure_default_reminder_template(company)
            self.fields["reminder_template"].queryset = (
                ReminderTemplate.objects.filter(company=company)
                .order_by("-is_default", "name")
            )
        else:
            self.fields["reminder_template"].queryset = ReminderTemplate.objects.none()
        self.fields["reminder_template"].required = False
        self.fields["reminder_template"].label = "Reminder cycle"
        self.fields["reminder_template"].empty_label = "Use company default"
        self.fields["reminder_template"].help_text = (
            "Choose which renewal reminder schedule this client follows."
        )


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
        wanted = asset_type_prefilter.strip()
        pq = pq.annotate(
            trimmed_category=Trim("category"),
            trimmed_asset_type=Trim("asset_type"),
        ).filter(
            Q(trimmed_category__iexact=wanted) | Q(trimmed_asset_type__iexact=wanted)
        )
    if data is not None:
        cat = (data.get("category_filter") or "").strip()
        cn = (data.get("company_name_filter") or "").strip()
        if cat:
            pq = pq.annotate(
                trimmed_category=Trim("category"),
                trimmed_asset_type=Trim("asset_type"),
            ).filter(
                Q(trimmed_category__iexact=cat) | Q(trimmed_asset_type__iexact=cat)
            )
        if cn:
            pq = pq.annotate(trimmed_company_name=Trim("company_name")).filter(
                trimmed_company_name__iexact=cn
            )
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


class CaseLockPublicForm(forms.ModelForm):
    class Meta:
        model = CaseLockSubmission
        fields = (
            "product",
            "client_name",
            "client_gst",
            "product_quantity",
        )
        labels = {
            "product": "Product",
            "client_name": "Customer name",
            "client_gst": "GST number",
            "product_quantity": "Quantity",
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        products = CaseLockProduct.objects.none()
        if company is not None:
            products = company.caselock_products.order_by("name")
        self.fields["product"] = forms.ModelChoiceField(
            queryset=products,
            required=True,
            empty_label="Select a product",
            widget=forms.Select(attrs={"class": "cl-product-native", "aria-hidden": "true"}),
        )
        self.fields["client_name"].required = True
        self.fields["client_gst"].required = True
        self.fields["product_quantity"].widget.attrs.setdefault("min", "1")
        self.fields["product_quantity"].initial = 1
        self.fields["client_name"].widget.attrs.setdefault("placeholder", "Customer name")
        self.fields["client_gst"].widget.attrs.setdefault("placeholder", "GST number")


class CaseLockProductForm(forms.ModelForm):
    class Meta:
        model = CaseLockProduct
        fields = ("name",)
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Product name"}),
        }

    def __init__(self, *args, company=None, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)
        self.fields["name"].label = "Product name"

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Enter a product name.")
        qs = CaseLockProduct.objects.filter(
            company=self.company, name__iexact=name
        )
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if self.company and qs.exists():
            raise forms.ValidationError("This product already exists.")
        return name


class RenewalConfirmLineForm(forms.Form):
    line_id = forms.IntegerField(widget=forms.HiddenInput)
    renewal_count = forms.IntegerField(
        min_value=0,
        label="Renewal count",
        widget=forms.NumberInput(
            attrs={
                "min": 0,
                "placeholder": "Qty",
                "aria-label": "Renewal quantity",
            }
        ),
    )


RenewalConfirmLineFormSet = formset_factory(RenewalConfirmLineForm, extra=0)


class RenewalNewProductForm(forms.Form):
    product_id = forms.IntegerField(widget=forms.HiddenInput)
    quantity = forms.IntegerField(
        min_value=1,
        initial=1,
        label="Quantity",
        widget=forms.NumberInput(
            attrs={
                "min": 1,
                "placeholder": "Qty",
                "aria-label": "New product quantity",
            }
        ),
    )


class BaseRenewalNewProductFormSet(BaseFormSet):
    def __init__(self, *args, company=None, excluded_product_ids=None, **kwargs):
        self.company = company
        self.excluded_product_ids = set(excluded_product_ids or [])
        super().__init__(*args, **kwargs)

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        valid_ids = set()
        if self.company is not None:
            valid_ids = set(
                Product.objects.filter(company=self.company).values_list("pk", flat=True)
            )
        seen = set()
        for form in self.forms:
            if not form.cleaned_data:
                continue
            product_id = form.cleaned_data.get("product_id")
            if product_id is None:
                continue
            if product_id in self.excluded_product_ids:
                form.add_error(
                    "product_id",
                    "This product is already in your renewal list.",
                )
                continue
            if valid_ids and product_id not in valid_ids:
                form.add_error("product_id", "Select a product from the list.")
                continue
            if product_id in seen:
                form.add_error("product_id", "This product was already added.")
                continue
            seen.add(product_id)


RenewalNewProductFormSet = formset_factory(
    RenewalNewProductForm,
    formset=BaseRenewalNewProductFormSet,
    extra=0,
    max_num=50,
    validate_max=True,
)


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


class SubscriptionRenewForm(forms.Form):
    start_date = forms.DateField(
        label="New start date",
        widget=forms.DateInput(
            attrs={"type": "date", "placeholder": "dd-mm-yyyy"},
            format="%Y-%m-%d",
        ),
    )
    end_date = forms.DateField(
        label="New end date",
        required=False,
        widget=forms.DateInput(
            attrs={"type": "date", "placeholder": "dd-mm-yyyy"},
            format="%Y-%m-%d",
        ),
    )
    quantity = forms.IntegerField(
        label="New quantity",
        min_value=0,
        widget=forms.NumberInput(attrs={"min": 0, "placeholder": "Qty"}),
    )
    billing_amount = forms.DecimalField(
        label="New price",
        min_value=0,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(
            attrs={"min": 0, "step": "0.01", "placeholder": "0.00"}
        ),
    )

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")
        if start and end and end < start:
            raise forms.ValidationError("End date must be on or after the start date.")
        return cleaned
