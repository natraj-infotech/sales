from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

handler404 = "accounts.error_views.page_not_found"
handler500 = "accounts.error_views.server_error"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("helpdesk/", include("helpdesk.urls")),
    path("whatsapp/", include("whatsapp.urls")),
    path("", include("accounts.urls")),
    path("", include("business.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
