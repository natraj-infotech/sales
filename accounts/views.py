from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .forms import CompanyProfileForm
from .models import CompanyProfile


def root_redirect(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return redirect("account_login")


@login_required
def company_setup(request):
    profile = CompanyProfile.objects.filter(user=request.user).first()

    if request.method == "POST":
        form = CompanyProfileForm(request.POST, instance=profile)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.user = request.user
            obj.save()
            return redirect("dashboard")
    else:
        form = CompanyProfileForm(instance=profile)

    return render(
        request,
        "accounts/company_setup.html",
        {"form": form, "editing": profile is not None},
    )
