from django.urls import path

from . import views

urlpatterns = [
    path("", views.root_redirect, name="root"),
    path("company/setup/", views.company_setup, name="company_setup"),
]
