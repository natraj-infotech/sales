from django import forms
from django.contrib.auth import get_user_model

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email

from business.expiry_notifications import (
    VALID_NOTIFY_DAYS,
    parse_extra_emails,
    parse_notify_days,
)

from .models import CompanyMember, CompanyProfile

User = get_user_model()

EXPIRY_DAY_CHOICES = [
    (str(days), f"{days} days before expiry") for days in sorted(VALID_NOTIFY_DAYS)
]


class CompanyProfileForm(forms.ModelForm):
    expiry_notify_day_choices = forms.MultipleChoiceField(
        label="Notify at these day thresholds",
        choices=EXPIRY_DAY_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Client reminders are sent once per subscription at each selected threshold. "
        "The team digest lists all subscriptions expiring within the largest selected window.",
    )

    class Meta:
        model = CompanyProfile
        fields = (
            "company_name",
            "company_logo",
            "phone",
            "email",
            "address",
            "gst_number",
            "cin_number",
            "support_email",
            "support_email_app_password",
            "outgoing_email",
            "outgoing_email_app_password",
            "regards_email",
            "regards_contact",
            "expiry_email_initial_paragraph",
            "expiry_email_end_paragraph",
            "expiry_client_emails_enabled",
            "expiry_team_digest_enabled",
            "expiry_team_extra_emails",
        )
        widgets = {
            "address": forms.Textarea(attrs={"rows": 4}),
            "company_logo": forms.FileInput(
                attrs={"accept": "image/png,image/jpeg,image/webp,image/gif"}
            ),
            "regards_email": forms.Textarea(
                attrs={"rows": 3, "placeholder": "one@example.com\ntwo@example.com"}
            ),
            "expiry_email_initial_paragraph": forms.Textarea(attrs={"rows": 3}),
            "expiry_email_end_paragraph": forms.Textarea(attrs={"rows": 3}),
            "expiry_team_extra_emails": forms.Textarea(attrs={"rows": 3}),
            "support_email_app_password": forms.PasswordInput(
                render_value=False, attrs={"autocomplete": "new-password"}
            ),
            "outgoing_email_app_password": forms.PasswordInput(
                render_value=False, attrs={"autocomplete": "new-password"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_support_email_app_password = (
            self.instance.support_email_app_password if self.instance.pk else ""
        )
        self._original_outgoing_email_app_password = (
            self.instance.outgoing_email_app_password if self.instance.pk else ""
        )
        self.fields["company_logo"].label = "Company logo"
        self.fields["company_logo"].help_text = (
            "Used in the header of client subscription expiry emails "
            "(PNG, JPG, WEBP, or GIF)."
        )
        self.fields["regards_email"].label = "Regards emails"
        self.fields["regards_email"].help_text = (
            "Shown under Best Regards in client expiry emails. "
            "Add multiple addresses, one per line or comma-separated."
        )
        self.fields["regards_contact"].label = "Regards contact details"
        self.fields["regards_contact"].help_text = (
            "Phone or other contact shown under Best Regards. Leave blank to hide."
        )
        self.fields["expiry_email_initial_paragraph"].label = "Initial paragraph"
        self.fields["expiry_email_initial_paragraph"].help_text = (
            "Shown above the product table in client expiry emails. "
            "Leave blank to keep the default line."
        )
        self.fields["expiry_email_initial_paragraph"].widget.attrs["placeholder"] = (
            "Kindly renew it to ensure there is no interruption to your services."
        )
        self.fields["expiry_email_end_paragraph"].label = "End paragraph"
        self.fields["expiry_email_end_paragraph"].help_text = (
            "Shown below the product table in client expiry emails. "
            "Leave blank to keep the default line."
        )
        self.fields["expiry_email_end_paragraph"].widget.attrs["placeholder"] = (
            "We request you to share your confirmation at the earliest so that we can "
            "initiate the renewal process and ensure uninterrupted service."
        )
        self.fields["expiry_client_emails_enabled"].label = "Email clients before expiry"
        self.fields["expiry_client_emails_enabled"].help_text = (
            "Send renewal reminders to the client email on file when a subscription "
            "reaches a selected day threshold."
        )
        self.fields["expiry_team_digest_enabled"].label = "Daily team digest"
        self.fields["expiry_team_digest_enabled"].help_text = (
            "Send one daily email with an Excel attachment to the company owner, "
            "sales team, and any extra addresses below listing subscriptions "
            "expiring soon."
        )
        self.fields["expiry_team_extra_emails"].label = "Extra team digest recipients"
        if self.instance.pk:
            for name in (
                "support_email_app_password",
                "outgoing_email_app_password",
            ):
                self.fields[name].required = False
                self.fields[name].help_text = "Leave blank to keep the saved app password."
            selected = [
                str(day) for day in parse_notify_days(self.instance.expiry_notify_at_days)
            ]
            self.fields["expiry_notify_day_choices"].initial = selected

    def clean_regards_email(self):
        raw = (self.cleaned_data.get("regards_email") or "").strip()
        emails = parse_extra_emails(raw)
        invalid = []
        for addr in emails:
            try:
                validate_email(addr)
            except DjangoValidationError:
                invalid.append(addr)
        if invalid:
            raise forms.ValidationError(
                "Invalid regards email: " + ", ".join(invalid)
            )
        return "\n".join(emails)

    def clean_expiry_notify_day_choices(self):
        selected = self.cleaned_data.get("expiry_notify_day_choices") or []
        days: list[int] = []
        for raw in selected:
            try:
                value = int(raw)
            except (TypeError, ValueError) as exc:
                raise forms.ValidationError("Invalid day threshold selected.") from exc
            if value not in VALID_NOTIFY_DAYS:
                raise forms.ValidationError(f"{value} is not an allowed threshold.")
            if value not in days:
                days.append(value)
        if not days and (
            self.cleaned_data.get("expiry_client_emails_enabled")
            or self.cleaned_data.get("expiry_team_digest_enabled")
        ):
            raise forms.ValidationError(
                "Select at least one day threshold when expiry notifications are enabled."
            )
        return selected

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.instance.pk:
            if not self.cleaned_data.get("support_email_app_password"):
                instance.support_email_app_password = (
                    self._original_support_email_app_password
                )
            if not self.cleaned_data.get("outgoing_email_app_password"):
                instance.outgoing_email_app_password = (
                    self._original_outgoing_email_app_password
                )
        selected_days = self.cleaned_data.get("expiry_notify_day_choices") or []
        if selected_days:
            ordered = sorted({int(day) for day in selected_days}, reverse=True)
            instance.expiry_notify_at_days = ",".join(str(day) for day in ordered)
        if commit:
            instance.save()
        return instance


class AddCompanyMemberForm(forms.Form):
    email = forms.EmailField(
        label="Teammate email",
        help_text="The person must already have a Tracker365 account.",
    )
    sales_access = forms.BooleanField(
        label="Sales module access",
        required=False,
        initial=True,
        help_text="Allow access to subscriptions, clients, products, vendors, and dashboard.",
    )
    can_grant_sales = forms.BooleanField(
        label="Can manage sales access",
        required=False,
        help_text="Allow this person to grant sales module access to others.",
    )

    def __init__(self, *args, company=None, requester_is_owner=True, **kwargs):
        self.company = company
        self.requester_is_owner = requester_is_owner
        super().__init__(*args, **kwargs)
        if not requester_is_owner:
            self.fields.pop("can_grant_sales", None)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist as exc:
            raise forms.ValidationError(
                "No account found with that email. Ask them to sign up first."
            ) from exc

        if self.company and self.company.user_id == user.pk:
            raise forms.ValidationError("The company owner is already part of this team.")

        if (
            self.company
            and CompanyMember.objects.filter(company=self.company, user=user).exists()
        ):
            raise forms.ValidationError("This person is already on your team.")

        existing_company = CompanyProfile.objects.filter(user=user).first()
        if existing_company and existing_company.pk != self.company.pk:
            raise forms.ValidationError(
                "This person already owns another company account."
            )

        other_membership = (
            CompanyMember.objects.filter(user=user)
            .exclude(company=self.company)
            .first()
        )
        if other_membership:
            raise forms.ValidationError(
                "This person is already linked to another company."
            )

        self.cleaned_data["user"] = user
        return email

    def save(self):
        user = self.cleaned_data["user"]
        can_grant = (
            self.requester_is_owner
            and self.cleaned_data.get("can_grant_sales", False)
        )
        sales_access = self.cleaned_data.get("sales_access", False) or can_grant
        return CompanyMember.objects.create(
            company=self.company,
            user=user,
            role=CompanyMember.Role.MEMBER,
            sales_access=sales_access,
            can_grant_sales=can_grant,
        )


class UpdateCompanyMemberForm(forms.Form):
    sales_access = forms.BooleanField(required=False)
    can_grant_sales = forms.BooleanField(required=False)

    def __init__(self, *args, member=None, requester_is_owner=True, **kwargs):
        self.member = member
        self.requester_is_owner = requester_is_owner
        super().__init__(*args, **kwargs)
        if not requester_is_owner:
            self.fields.pop("can_grant_sales", None)

    def save(self):
        if self.member.role == CompanyMember.Role.OWNER:
            return self.member

        self.member.sales_access = self.cleaned_data.get("sales_access", False)
        if self.requester_is_owner:
            self.member.can_grant_sales = self.cleaned_data.get(
                "can_grant_sales", False
            )
        elif not self.member.sales_access:
            self.member.can_grant_sales = False
        if self.member.can_grant_sales:
            self.member.sales_access = True
        self.member.save(
            update_fields=["sales_access", "can_grant_sales"]
            if self.requester_is_owner
            else ["sales_access"]
        )
        return self.member
