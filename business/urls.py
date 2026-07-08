from django.urls import path

from . import views

urlpatterns = [
    path(
        "caselock/fill/<uuid:invite_id>/",
        views.caselock_public_form,
        name="caselock_public_form",
    ),
    path("caselock/inboxes/", views.caselock_inbox_list, name="caselock_inbox_list"),
    path("caselock/inboxes/add/", views.caselock_inbox_add, name="caselock_inbox_add"),
    path(
        "caselock/inboxes/<int:pk>/edit/",
        views.caselock_inbox_edit,
        name="caselock_inbox_edit",
    ),
    path(
        "caselock/submissions/",
        views.caselock_submissions,
        name="caselock_submissions",
    ),
    path(
        "caselock/submissions/<int:pk>/status/",
        views.caselock_submission_status_update,
        name="caselock_submission_status_update",
    ),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("vendors/", views.vendor_list, name="vendor_list"),
    path("vendors/bulk-delete/", views.vendor_bulk_delete, name="vendor_bulk_delete"),
    path("vendors/export/", views.vendor_export, name="vendor_export"),
    path("vendors/import/", views.vendor_import, name="vendor_import"),
    path("vendors/add/", views.vendor_add, name="vendor_add"),
    path("vendors/<int:pk>/edit/", views.vendor_edit, name="vendor_edit"),
    path("vendors/<int:pk>/delete/", views.vendor_delete, name="vendor_delete"),
    path("clients/", views.client_list, name="client_list"),
    path("clients/export/", views.client_export, name="client_export"),
    path("clients/import/", views.client_import, name="client_import"),
    path("clients/add/", views.client_add, name="client_add"),
    path("clients/<int:pk>/edit/", views.client_edit, name="client_edit"),
    path("clients/<int:pk>/delete/", views.client_delete, name="client_delete"),
    path("products/", views.product_list, name="product_list"),
    path("products/bulk-delete/", views.product_bulk_delete, name="product_bulk_delete"),
    path("products/export/", views.product_export, name="product_export"),
    path("products/import/", views.product_import, name="product_import"),
    path(
        "products/import/advanced/",
        views.product_import_advanced,
        name="product_import_advanced",
    ),
    path(
        "products/import/map/",
        views.product_import_map,
        name="product_import_map",
    ),
    path(
        "products/import/template/",
        views.product_import_template,
        name="product_import_template",
    ),
    path("products/add/", views.product_add, name="product_add"),
    path("products/<int:pk>/edit/", views.product_edit, name="product_edit"),
    path("products/<int:pk>/delete/", views.product_delete, name="product_delete"),
    path("subscriptions/", views.subscription_list, name="subscription_list"),
    path("subscriptions/import/", views.subscription_import, name="subscription_import"),
    path(
        "subscriptions/import/map/",
        views.subscription_import_map,
        name="subscription_import_map",
    ),
    path(
        "subscriptions/import/template/",
        views.subscription_import_template,
        name="subscription_import_template",
    ),
    path("subscriptions/add/", views.subscription_add, name="subscription_add"),
    path(
        "subscriptions/<int:pk>/edit/",
        views.subscription_edit,
        name="subscription_edit",
    ),
    path(
        "subscriptions/<int:pk>/renew/",
        views.subscription_renew,
        name="subscription_renew",
    ),
    path("support/tickets/", views.support_ticket_list, name="support_ticket_list"),
    path("support/tickets/add/", views.support_ticket_add, name="support_ticket_add"),
    path(
        "support/tickets/<int:pk>/",
        views.support_ticket_detail,
        name="support_ticket_detail",
    ),
    path(
        "support/tickets/<int:pk>/update/",
        views.support_ticket_update,
        name="support_ticket_update",
    ),
]
