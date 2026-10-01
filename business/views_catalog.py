"""Company catalog configuration (master → category → subcategory → product)."""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from accounts.permissions import get_company_for_user, sales_module_required

from .catalog import CatalogCategory, CatalogMasterCategory, CatalogProduct, CatalogSubcategory
from .forms_catalog import CatalogCategoryForm, CatalogProductForm, CatalogSubcategoryForm


def _company(request):
    return get_company_for_user(request.user)


@sales_module_required
def catalog_hub(request):
    company = _company(request)
    masters = []
    for key, label in CatalogMasterCategory.choices:
        cats = CatalogCategory.objects.filter(company=company, master_category=key)
        masters.append(
            {
                "key": key,
                "label": label,
                "category_count": cats.count(),
            }
        )
    return render(
        request,
        "business/catalog_hub.html",
        {"company": company, "masters": masters},
    )


@sales_module_required
def catalog_category_list(request, master_key: str):
    company = _company(request)
    if master_key not in CatalogMasterCategory.values:
        messages.error(request, "Invalid master category.")
        return redirect("catalog_hub")
    categories = CatalogCategory.objects.filter(
        company=company, master_category=master_key
    ).order_by("sort_order", "name")
    return render(
        request,
        "business/catalog_category_list.html",
        {
            "company": company,
            "master_key": master_key,
            "master_label": CatalogMasterCategory(master_key).label,
            "categories": categories,
        },
    )


@sales_module_required
def catalog_category_add(request):
    return catalog_category_edit(request, pk=None)


@sales_module_required
def catalog_category_edit(request, pk: int | None = None):
    company = _company(request)
    instance = None
    if pk is not None:
        instance = get_object_or_404(CatalogCategory, pk=pk, company=company)
    if request.method == "POST":
        form = CatalogCategoryForm(request.POST, instance=instance)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.company = company
            obj.save()
            messages.success(request, "Category saved.")
            return redirect("catalog_category_list", master_key=obj.master_category)
    else:
        initial = {}
        master = (request.GET.get("master") or "").strip()
        if master in CatalogMasterCategory.values and instance is None:
            initial["master_category"] = master
        form = CatalogCategoryForm(instance=instance, initial=initial)
    return render(
        request,
        "business/catalog_category_form.html",
        {"form": form, "instance": instance, "company": company},
    )


@sales_module_required
def catalog_category_delete(request, pk: int):
    company = _company(request)
    cat = get_object_or_404(CatalogCategory, pk=pk, company=company)
    master = cat.master_category
    if request.method == "POST":
        cat.delete()
        messages.success(request, "Category deleted.")
        return redirect("catalog_category_list", master_key=master)
    return render(
        request,
        "business/catalog_confirm_delete.html",
        {"object_label": str(cat), "cancel_url": reverse("catalog_category_list", args=[master])},
    )


@sales_module_required
def catalog_subcategory_list(request, category_pk: int):
    company = _company(request)
    category = get_object_or_404(CatalogCategory, pk=category_pk, company=company)
    subcategories = category.subcategories.order_by("sort_order", "name")
    return render(
        request,
        "business/catalog_subcategory_list.html",
        {
            "company": company,
            "category": category,
            "subcategories": subcategories,
        },
    )


@sales_module_required
def catalog_subcategory_add(request, category_pk: int):
    return catalog_subcategory_edit(request, category_pk, pk=None)


@sales_module_required
def catalog_subcategory_edit(request, category_pk: int, pk: int | None = None):
    company = _company(request)
    category = get_object_or_404(CatalogCategory, pk=category_pk, company=company)
    instance = None
    if pk is not None:
        instance = get_object_or_404(CatalogSubcategory, pk=pk, category=category)
    if request.method == "POST":
        form = CatalogSubcategoryForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.category = category
            obj.save()
            messages.success(request, "Subcategory saved.")
            return redirect("catalog_subcategory_list", category_pk=category.pk)
    else:
        form = CatalogSubcategoryForm(instance=instance)
    return render(
        request,
        "business/catalog_subcategory_form.html",
        {"form": form, "category": category, "instance": instance},
    )


@sales_module_required
def catalog_subcategory_delete(request, category_pk: int, pk: int):
    company = _company(request)
    category = get_object_or_404(CatalogCategory, pk=category_pk, company=company)
    sub = get_object_or_404(CatalogSubcategory, pk=pk, category=category)
    if request.method == "POST":
        sub.delete()
        messages.success(request, "Subcategory deleted.")
        return redirect("catalog_subcategory_list", category_pk=category.pk)
    return render(
        request,
        "business/catalog_confirm_delete.html",
        {
            "object_label": str(sub),
            "cancel_url": reverse("catalog_subcategory_list", args=[category.pk]),
        },
    )


@sales_module_required
def catalog_product_list(request, subcategory_pk: int):
    company = _company(request)
    sub = get_object_or_404(
        CatalogSubcategory.objects.select_related("category"),
        pk=subcategory_pk,
        category__company=company,
    )
    products = sub.products.order_by("sort_order", "brand", "product_name")
    return render(
        request,
        "business/catalog_product_list.html",
        {"company": company, "subcategory": sub, "products": products},
    )


@sales_module_required
def catalog_product_add(request, subcategory_pk: int):
    return catalog_product_edit(request, subcategory_pk, pk=None)


@sales_module_required
def catalog_product_edit(request, subcategory_pk: int, pk: int | None = None):
    company = _company(request)
    sub = get_object_or_404(
        CatalogSubcategory,
        pk=subcategory_pk,
        category__company=company,
    )
    instance = None
    if pk is not None:
        instance = get_object_or_404(CatalogProduct, pk=pk, subcategory=sub)
    if request.method == "POST":
        form = CatalogProductForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.subcategory = sub
            obj.save()
            messages.success(request, "Product saved.")
            return redirect("catalog_product_list", subcategory_pk=sub.pk)
    else:
        form = CatalogProductForm(instance=instance)
    return render(
        request,
        "business/catalog_product_form.html",
        {"form": form, "subcategory": sub, "instance": instance},
    )


@sales_module_required
def catalog_product_delete(request, subcategory_pk: int, pk: int):
    company = _company(request)
    sub = get_object_or_404(CatalogSubcategory, pk=subcategory_pk, category__company=company)
    product = get_object_or_404(CatalogProduct, pk=pk, subcategory=sub)
    if request.method == "POST":
        product.delete()
        messages.success(request, "Product deleted.")
        return redirect("catalog_product_list", subcategory_pk=sub.pk)
    return render(
        request,
        "business/catalog_confirm_delete.html",
        {
            "object_label": product.display_label(),
            "cancel_url": reverse("catalog_product_list", args=[sub.pk]),
        },
    )
