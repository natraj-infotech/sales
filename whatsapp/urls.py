from django.urls import path

from . import views

urlpatterns = [
    path("webhook/", views.webhook, name="whatsapp_webhook"),
    path("flow/", views.flow_data_exchange, name="whatsapp_flow"),
]
