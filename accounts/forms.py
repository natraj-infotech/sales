from django import forms
from django.contrib.auth import get_user_model

from .models import CompanyMember, CompanyProfile

User = get_user_model()


class CompanyProfileForm(forms.ModelForm):
    class Meta:
        model = CompanyProfile
        fields = (
            "company_name",
            "phone",
            "email",
            "address",
            "gst_number",
            "cin_number",
            "support_email",
            "support_email_app_password",
            "outgoing_email",
            "outgoing_email_app_password",
        )
        widgets = {
            "address": forms.Textarea(attrs={"rows": 4}),
            "support_email_app_password": forms.PasswordInput(
                render_value=False, attrs={"autocomplete": "new-password"}
            ),
            "outgoing_email_app_password": forms.PasswordInput(
                render_value=False, attrs={"autocomplete": "new-password"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            for name in (
                "support_email_app_password",
                "outgoing_email_app_password",
            ):
                self.fields[name].required = False
                self.fields[name].help_text = "Leave blank to keep the saved app password."

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.instance.pk:
            if not self.cleaned_data.get("support_email_app_password"):
                instance.support_email_app_password = (
                    self.instance.support_email_app_password
                )
            if not self.cleaned_data.get("outgoing_email_app_password"):
                instance.outgoing_email_app_password = (
                    self.instance.outgoing_email_app_password
                )
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
