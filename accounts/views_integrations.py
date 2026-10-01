"""Company integrations (WhatsApp lines) — configuration UI."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse

from accounts.permissions import company_owner_required, get_company_for_user
from whatsapp.models import WhatsAppAccount


class WhatsAppAccountForm(forms.ModelForm):
    class Meta:
        model = WhatsAppAccount
        fields = [
            "label",
            "phone_number_id",
            "waba_id",
            "display_phone",
            "access_token",
            "is_active",
        ]
        widgets = {
            "label": forms.TextInput(attrs={"placeholder": "e.g. Main sales line"}),
            "phone_number_id": forms.TextInput(attrs={"placeholder": "From Meta API Setup"}),
            "waba_id": forms.TextInput(attrs={"placeholder": "WhatsApp Business Account ID"}),
            "display_phone": forms.TextInput(attrs={"placeholder": "+91 …"}),
            "access_token": forms.Textarea(
                attrs={"rows": 4, "autocomplete": "off", "placeholder": "System user token (optional if set in .env)"},
            ),
        }
        help_texts = {
            "phone_number_id": "Required — must match the ID Meta sends on webhooks.",
            "access_token": "Optional if WHATSAPP_ACCESS_TOKEN is set in server .env.",
        }
        labels = {
            "waba_id": "WABA ID",
            "phone_number_id": "Phone number ID",
        }


@company_owner_required
def company_integrations(request):
    company = get_company_for_user(request.user)
    accounts = WhatsAppAccount.objects.filter(company=company).order_by("label", "id")
    edit_id = (request.GET.get("edit") or "").strip()
    instance = None
    if edit_id.isdigit():
        instance = accounts.filter(pk=int(edit_id)).first()

    if request.method == "POST":
        if request.POST.get("action") == "delete" and request.POST.get("account_id"):
            acc = accounts.filter(pk=request.POST.get("account_id")).first()
            if acc:
                acc.delete()
                messages.success(request, "WhatsApp line removed.")
            return redirect("company_integrations")
        pk = request.POST.get("account_id")
        if pk and str(pk).isdigit():
            instance = accounts.filter(pk=int(pk)).first()
        form = WhatsAppAccountForm(request.POST, instance=instance)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.company = company
            obj.save()
            messages.success(request, "WhatsApp line saved.")
            return redirect("company_integrations")
    else:
        form = WhatsAppAccountForm(instance=instance)

    webhook_url = request.build_absolute_uri(reverse("whatsapp_webhook"))

    return render(
        request,
        "accounts/company_integrations.html",
        {
            "company": company,
            "accounts": accounts,
            "form": form,
            "editing": instance,
            "webhook_url": webhook_url,
        },
    )
