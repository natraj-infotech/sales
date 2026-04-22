from django import forms

from .models import CompanyProfile


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
