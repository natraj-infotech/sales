from django.shortcuts import redirect


class RequireCompanyProfileMiddleware:
    """Send authenticated users without a company profile to setup."""

    PREFIXES = (
        "/admin/",
        "/accounts/",
        "/static/",
        "/media/",
        "/caselock/fill/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if path.startswith("/company/setup"):
            return self.get_response(request)

        exempt = any(path.startswith(p) for p in self.PREFIXES)
        if request.user.is_authenticated and not exempt:
            if not _user_has_company(request.user):
                return redirect("company_setup")

        return self.get_response(request)


def _user_has_company(user):
    from django.core.exceptions import ObjectDoesNotExist

    try:
        user.company_profile
        return True
    except ObjectDoesNotExist:
        return False
