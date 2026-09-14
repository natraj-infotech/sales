from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string


def page_not_found(request, exception):
    return render(request, "404.html", status=404)


def server_error(request):
    html = render_to_string(
        "500.html",
        {"error_id": getattr(request, "error_id", "")},
    )
    return HttpResponse(html, status=500)
