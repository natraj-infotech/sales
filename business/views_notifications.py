"""Sidebar bell: list, feed, and open-through to the related record."""

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from business.models import TeamNotification
from business.team_notifications import safe_target_path, visible_since

FEED_LIMIT = 15


def _queryset(user):
    return TeamNotification.objects.filter(
        recipient=user,
        created_at__gte=visible_since(),
    )


def _payload(note: TeamNotification) -> dict:
    return {
        "id": note.pk,
        "kind": note.kind,
        "kind_label": note.get_kind_display(),
        "title": note.title,
        "body": note.body,
        "is_read": note.is_read,
        "created_at": note.created_at.isoformat(),
        "open_url": reverse("notification_open", args=[note.pk]),
    }


@login_required
def notification_list(request):
    notes = list(_queryset(request.user)[:100])
    return render(
        request,
        "business/notification_list.html",
        {
            "notifications": notes,
            "unread_count": sum(1 for note in notes if not note.is_read),
        },
    )


@login_required
@require_http_methods(["GET"])
def notification_feed(request):
    qs = _queryset(request.user)
    unread = qs.filter(is_read=False).count()
    items = [_payload(note) for note in qs[:FEED_LIMIT]]
    return JsonResponse({"ok": True, "unread": unread, "items": items})


@login_required
@require_http_methods(["GET"])
def notification_open(request, pk):
    note = get_object_or_404(TeamNotification, pk=pk, recipient=request.user)
    if not note.is_read:
        note.is_read = True
        note.read_at = timezone.now()
        note.save(update_fields=["is_read", "read_at"])
    target = safe_target_path(note.target_url) or reverse("notification_list")
    return redirect(target)


@login_required
@require_http_methods(["POST"])
def notification_mark_all_read(request):
    _queryset(request.user).filter(is_read=False).update(
        is_read=True,
        read_at=timezone.now(),
    )
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True})
    return redirect("notification_list")
