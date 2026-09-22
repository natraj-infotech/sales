import logging
import uuid

from django.conf import settings
from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.http import Http404
from django.shortcuts import redirect

logger = logging.getLogger("accounts.exceptions")

_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


class PreferHttpsExceptLocalhostMiddleware:
    """Use HTTP only on loopback; treat every other host as HTTPS.

    Django behind sslip.io / nginx often sees the request as HTTP, so
    allauth would send http://... Microsoft callbacks (AADSTS50011).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            raw_host = request.get_host() or ""
        except SuspiciousOperation:
            return self.get_response(request)
        if raw_host.startswith("[") and "]" in raw_host:
            host = raw_host[1 : raw_host.index("]")].lower()
        else:
            host = raw_host.split(":")[0].strip().lower()
        extra = getattr(settings, "LOCAL_HOSTNAMES", ())
        local_hosts = _LOCAL_HOSTNAMES.union(extra)
        if host and host not in local_hosts:
            request.META["HTTP_X_FORWARDED_PROTO"] = "https"
            request.META["wsgi.url_scheme"] = "https"
        return self.get_response(request)


class ExceptionLoggingMiddleware:
    """Log uncaught view errors with request path, user, and company."""

    _SKIP = (Http404, PermissionDenied, SuspiciousOperation)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, self._SKIP):
            return None
        error_id = uuid.uuid4().hex[:8]
        request.error_id = error_id
        user = getattr(request, "user", None)
        user_id = getattr(user, "pk", None) if user is not None else None
        logger.exception(
            "Unhandled error id=%s method=%s path=%s user_id=%s company_id=%s",
            error_id,
            getattr(request, "method", ""),
            request.get_full_path() if hasattr(request, "get_full_path") else "",
            user_id,
            _company_id_for_user(user),
        )
        return None


def _company_id_for_user(user):
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    try:
        from .permissions import get_company_for_user

        company = get_company_for_user(user)
    except Exception:
        return None
    return getattr(company, "pk", None)


class RequireCompanyProfileMiddleware:
    """Send authenticated users without a company profile to setup."""

    PREFIXES = (
        "/admin/",
        "/accounts/",
        "/static/",
        "/media/",
        "/caselock/fill/",
        "/renewal/",
        "/vendor-price-submit/",
        "/privacy-policy/",
        "/whatsapp/",
    )

    EXACT_PATHS = (
        "/company/access/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if path.startswith("/company/setup") or path in self.EXACT_PATHS:
            return self.get_response(request)

        exempt = any(path.startswith(p) for p in self.PREFIXES)
        if request.user.is_authenticated and not exempt:
            if not _user_has_company(request.user):
                return redirect("company_setup")

        return self.get_response(request)


def _user_has_company(user):
    from django.core.exceptions import ObjectDoesNotExist

    from .models import CompanyMember

    try:
        user.company_profile
        return True
    except ObjectDoesNotExist:
        pass
    return CompanyMember.objects.filter(user=user).exists()
