from django import forms
from django.core.validators import FileExtensionValidator
from django.forms import BaseFormSet, formset_factory
from django.db.models import Q
from django.db.models.functions import Trim

from .models import (
    _add_years,
    Vendor,
    Client,
    CaseLockInbox,
    CaseLockProduct,
    CaseLockSubmission,
    ClientSubscription,
    Lead,
    Product,
    SupportTicket,
    SupportSLASettings,
    TicketPriorityRule,
    RenewalSLASettings,
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


class RenewalSLASettingsForm(forms.ModelForm):
    class Meta:
        model = RenewalSLASettings
        fields = (
            "clock_mode",
            "target_hours",
            "work_start",
            "work_end",
            "work_monday",
            "work_tuesday",
            "work_wednesday",
            "work_thursday",
            "work_friday",
            "work_saturday",
            "work_sunday",
        )
        labels = {
            "clock_mode": "Count time",
            "target_hours": "Send quote within",
            "work_start": "From",
            "work_end": "To",
            "work_monday": "Mon",
            "work_tuesday": "Tue",
            "work_wednesday": "Wed",
            "work_thursday": "Thu",
            "work_friday": "Fri",
            "work_saturday": "Sat",
            "work_sunday": "Sun",
        }
        widgets = {
            "clock_mode": forms.RadioSelect,
            "target_hours": forms.NumberInput(attrs={"min": 1, "max": 168}),
            "work_start": forms.TimeInput(attrs={"type": "time"}),
            "work_end": forms.TimeInput(attrs={"type": "time"}),
        }
        help_texts = {
            "clock_mode": "",
            "target_hours": "",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["clock_mode"].help_text = ""
        self.fields["target_hours"].help_text = ""
        for name in ("work_start", "work_end"):
            self.fields[name].input_formats = ["%H:%M", "%H:%M:%S"]
            self.fields[name].widget.format = "%H:%M"

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("clock_mode")
        start = cleaned.get("work_start")
        end = cleaned.get("work_end")
        if mode == RenewalSLASettings.ClockMode.WORKING and start and end and end <= start:
            self.add_error("work_end", "Office end must be after office start.")
        if mode == RenewalSLASettings.ClockMode.WORKING:
            days = [
                cleaned.get("work_monday"),
                cleaned.get("work_tuesday"),
                cleaned.get("work_wednesday"),
                cleaned.get("work_thursday"),
                cleaned.get("work_friday"),
                cleaned.get("work_saturday"),
                cleaned.get("work_sunday"),
            ]
            if not any(days):
                self.add_error("work_monday", "Tick at least one working day.")
        return cleaned


class VendorForm(forms.ModelForm):
    catalog_companies = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Product company",
        help_text="Select one or more product companies this vendor can quote.",
    )

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
            "catalog_companies",
        )

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is None and getattr(self.instance, "company_id", None):
            company = self.instance.company
        choices = []
        seen = set()
        products = Product.objects.none()
        if company is not None:
            products = Product.objects.filter(company=company)
        for raw in products.order_by("company_name").values_list(
            "company_name", flat=True
        ):
            label = (raw or "").strip()
            key = label.lower()
            if label and key not in seen:
                seen.add(key)
                choices.append((label, label))
        if self.instance.pk:
            for raw in self.instance.catalog_companies or []:
                label = (raw or "").strip()
                key = label.lower()
                if label and key not in seen:
                    seen.add(key)
                    choices.append((label, label))
        choices.sort(key=lambda item: item[0].lower())
        self.fields["catalog_companies"].choices = choices
        if self.instance.pk and not self.is_bound:
            self.initial["catalog_companies"] = [
                (name or "").strip()
                for name in (self.instance.catalog_companies or [])
                if (name or "").strip()
            ]


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = (
            "name",
            "gst_number",
            "pan_number",
            "tenant_id",
            "address",
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
            "address": forms.Textarea(attrs={"rows": 3}),
            "renewal_emails_enabled": forms.CheckboxInput(),
        }

    def __init__(self, *args, company=None, **kwargs):
        from business.models import ReminderTemplate
        from business.reminder_templates import ensure_default_reminder_template

        self.company = company
        super().__init__(*args, **kwargs)
        for name in (
            "gst_number",
            "pan_number",
            "tenant_id",
            "address",
            "email",
            "email_2",
            "email_3",
            "phone",
            "phone_2",
            "phone_3",
        ):
            self.fields[name].required = False
        self.fields["pan_number"].widget.attrs.update(
            {"placeholder": "ABCDE1234F", "maxlength": "10", "style": "text-transform:uppercase;"}
        )
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

    def clean_pan_number(self):
        from business.models import PAN_FORMAT_ERROR, PAN_PATTERN, normalize_pan

        pan = normalize_pan(self.cleaned_data.get("pan_number"))
        if pan and not PAN_PATTERN.match(pan):
            raise forms.ValidationError(PAN_FORMAT_ERROR)
        return pan

    def clean_tenant_id(self):
        return (self.cleaned_data.get("tenant_id") or "").strip()


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
            "sku",
            "unit_price",
            "discounted_price",
            "description",
        )

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sku"].label = "Company Part Number"
        self.fields["hsn_code"].label = "HSN Code  (optional)"
        self.fields["hsn_code"].widget.attrs.setdefault("placeholder", "Enter if applicable")
        self.fields["tax"].label = "GST (%)"
        self.fields["depreciation"].label = "Depreciation Percentage (%)"
        self.fields["tax"].widget.attrs.setdefault("placeholder", "Enter GST %")
        self.fields["depreciation"].widget.attrs.setdefault("placeholder", "Enter depreciation %")
        self.fields["unit_price"].required = False
        self.fields["unit_price"].label = "Price"
        self.fields["unit_price"].widget.attrs.setdefault("placeholder", "Enter price")
        self.fields["unit_price"].widget.attrs.setdefault("min", "0")
        self.fields["unit_price"].widget.attrs.setdefault("step", "0.01")
        self.fields["discounted_price"].required = False
        self.fields["discounted_price"].label = "Discounted price"
        self.fields["discounted_price"].widget.attrs.setdefault(
            "placeholder", "Enter discounted price"
        )
        self.fields["discounted_price"].widget.attrs.setdefault("min", "0")
        self.fields["discounted_price"].widget.attrs.setdefault("step", "0.01")
        self.fields["description"].required = False
        self.fields["unit_price"].initial = self.fields["unit_price"].initial or 0
        self.fields["discounted_price"].initial = (
            self.fields["discounted_price"].initial or 0
        )
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
        if cleaned.get("discounted_price") in (None, ""):
            cleaned["discounted_price"] = 0
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
            "invoice_pdf",
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
            "invoice_pdf": forms.FileInput(
                attrs={"accept": "application/pdf,.pdf"}
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
        self.fields["invoice_number"].required = False
        self.fields["invoice_pdf"].label = "Invoice PDF copy"
        self.fields["invoice_pdf"].required = False
        self.fields["invoice_pdf"].help_text = "Optional. PDF only."
        self.fields["invoice_pdf"].validators.append(
            FileExtensionValidator(allowed_extensions=["pdf"])
        )
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
            "client_name": "Company name",
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
            error_messages={"required": "Select a product."},
            widget=forms.Select(
                attrs={
                    "class": "cl-product-native",
                    "aria-hidden": "true",
                    "required": True,
                }
            ),
        )
        for name in ("client_name", "client_gst", "product_quantity"):
            self.fields[name].required = True
        self.fields["client_name"].error_messages["required"] = "Enter the company name."
        self.fields["client_gst"].error_messages["required"] = "Enter the GST number."
        self.fields["product_quantity"].error_messages["required"] = "Enter the quantity."
        self.fields["product_quantity"].widget.attrs.update(
            {"min": "1", "required": True, "placeholder": "Quantity"}
        )
        self.fields["client_name"].widget.attrs.update(
            {"required": True, "placeholder": "Company name"}
        )
        self.fields["client_gst"].widget.attrs.update(
            {"required": True, "placeholder": "GST number"}
        )

    def clean_client_name(self):
        name = (self.cleaned_data.get("client_name") or "").strip()
        if not name:
            raise forms.ValidationError("Enter the company name.")
        return name

    def clean_client_gst(self):
        gst = (self.cleaned_data.get("client_gst") or "").strip()
        if not gst:
            raise forms.ValidationError("Enter the GST number.")
        return gst

    def clean_product_quantity(self):
        qty = self.cleaned_data.get("product_quantity")
        if qty is None or qty < 1:
            raise forms.ValidationError("Enter a quantity of at least 1.")
        return qty


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
    NEW_VENDOR = "__new__"

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
    vendor = forms.ChoiceField(
        label="New vendor",
        required=False,
    )
    invoice_number = forms.CharField(
        label="New invoice number",
        required=False,
        max_length=128,
    )
    invoice_pdf = forms.FileField(
        label="New invoice PDF",
        required=False,
        validators=[FileExtensionValidator(allowed_extensions=["pdf"])],
        widget=forms.FileInput(attrs={"accept": "application/pdf,.pdf"}),
    )
    duration = forms.IntegerField(
        label="Duration",
        min_value=1,
        max_value=30,
        initial=1,
        required=False,
        widget=forms.NumberInput(attrs={"min": 1, "max": 30, "step": 1}),
    )
    renew_other_plan = forms.BooleanField(required=False)
    product = forms.ChoiceField(
        label="New renewal product",
        required=False,
    )

    def __init__(self, *args, company=None, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)
        vendors = []
        products = []
        if company is not None:
            vendors = list(company.vendors.all().order_by("name"))
            products = list(company.products.all().order_by("unit_price", "name"))
        choices = [("", "---------")] + [
            (str(v.pk), v.name) for v in vendors
        ]
        choices.append((self.NEW_VENDOR, "+ Add new vendor"))
        self.fields["vendor"].choices = choices
        self.fields["product"].choices = [("", "Select a product")] + [
            (str(p.pk), p.name) for p in products
        ]

    def clean_renew_other_plan(self):
        raw = self.data.get("renew_other_plan")
        if isinstance(raw, bool):
            return raw
        return str(raw or "").strip().lower() in {"1", "true", "on", "yes"}

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        years = cleaned.get("duration") or 1
        try:
            years = int(years)
        except (TypeError, ValueError):
            years = 0
        if years < 1 or years > 30:
            self.add_error("duration", "Enter duration in years, from 1 to 30.")
        elif start:
            cleaned["duration"] = years
            cleaned["end_date"] = _add_years(start, years)
        end = cleaned.get("end_date")
        if start and end and end < start:
            raise forms.ValidationError("End date must be on or after the start date.")
        choice = (cleaned.get("vendor") or "").strip()
        cleaned["resolved_vendor"] = None
        if choice == self.NEW_VENDOR:
            self.add_error("vendor", "Add the vendor from the Add vendor page.")
        elif choice:
            try:
                pk = int(choice)
            except (TypeError, ValueError):
                self.add_error("vendor", "Select a valid vendor.")
            else:
                vendor = None
                if self.company is not None:
                    vendor = self.company.vendors.filter(pk=pk).first()
                if vendor is None:
                    self.add_error("vendor", "Select a valid vendor.")
                else:
                    cleaned["resolved_vendor"] = vendor
        cleaned["resolved_product"] = None
        product_choice = (cleaned.get("product") or "").strip()
        if cleaned.get("renew_other_plan"):
            if not product_choice:
                self.add_error("product", "Select a new renewal product.")
            else:
                try:
                    pk = int(product_choice)
                except (TypeError, ValueError):
                    self.add_error("product", "Select a valid product.")
                else:
                    product = None
                    if self.company is not None:
                        product = self.company.products.filter(pk=pk).first()
                    if product is None:
                        self.add_error("product", "Select a valid product.")
                    else:
                        cleaned["resolved_product"] = product
        return cleaned


class LeadForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = (
            "source",
            "contact_name",
            "company_name",
            "email",
            "phone",
            "requirement",
            "notes",
            "products",
            "status",
        )
        widgets = {
            "requirement": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 2}),
            "products": forms.SelectMultiple(attrs={"size": 8}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if company is not None:
            self.fields["products"].queryset = company.products.all().order_by("name")
        else:
            self.fields["products"].queryset = Product.objects.none()
        self.fields["products"].required = False
        self.fields["email"].required = False
        self.fields["phone"].required = False
        self.fields["company_name"].required = False
        self.fields["requirement"].required = False
        self.fields["notes"].required = False


class LeadStatusForm(forms.Form):
    status = forms.ChoiceField(choices=Lead.Status.choices)
