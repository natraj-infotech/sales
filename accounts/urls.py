from django.urls import path

from . import views

urlpatterns = [
    path("", views.root_redirect, name="root"),
    path("company/setup/", views.company_setup, name="company_setup"),
    path(
        "company/reminder-template/",
        views.reminder_template,
        name="reminder_template",
    ),
    path("company/zoho/connect/", views.zoho_connect, name="zoho_connect"),
    path("company/zoho/callback/", views.zoho_callback, name="zoho_callback"),
    path("company/zoho/disconnect/", views.zoho_disconnect, name="zoho_disconnect"),
    path(
        "company/expiry-notifications/send/",
        views.company_send_expiry_notifications,
        name="company_send_expiry_notifications",
    ),
    path("company/access/", views.access_pending, name="access_pending"),
    path("company/team/", views.company_team, name="company_team"),
]
