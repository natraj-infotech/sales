"""Email a ticket's public conversation to an address typed by staff."""

from __future__ import annotations

import logging
import os
import re

from django.utils.formats import date_format
from html import escape

from business.helpdesk_assign_mail import _smtp_credentials
from business.helpdesk_bridge import followups_as_comments
from business.outbound_mail import send_outbound, send_via_microsoft_graph

logger = logging.getLogger(__name__)

# Graph sendMail JSON requests fail above about 4 MB.
_MAX_ATTACHMENT_BYTES = 3_000_000


def _ticket_ref(ticket) -> str:
    return f"TK-{ticket.pk:05d}"


def _unique_filename(name: str, used: set[str]) -> str:
    base = os.path.basename((name or "attachment").replace("\\", "/"))
    base = re.sub(r"[\r\n\t]", " ", base).strip() or "attachment"
    if len(base) > 120:
        root, ext = os.path.splitext(base)
        base = root[:100] + ext[:20]
    candidate = base
    number = 2
    while candidate.lower() in used:
        root, ext = os.path.splitext(base)
        candidate = f"{root}-{number}{ext}"
        number += 1
    used.add(candidate.lower())
    return candidate


def _html_lines(text: str) -> str:
    lines = (text or "").splitlines() or [""]
    return "<br>".join(escape(line) for line in lines)


def build_conversation_summary(
    ticket,
) -> tuple[str, str, list[tuple[str, bytes, str]], list[str], bool]:
    """
    Public conversation only.

    Returns (plain text, html, file attachments, names left out for size, has messages).
    """
    from business.helpdesk_bridge import assignment_history

    comments = [
        comment
        for comment in followups_as_comments(ticket, load_files=True)
        if not comment.internal_note
    ]
    events = assignment_history(ticket)
    ref = _ticket_ref(ticket)
    title = (getattr(ticket, "title", None) or "Support ticket").strip()
    heading = f"{ref} — {title}"

    text_parts = [heading, ""]
    html_parts = [
        f"<h2 style=\"margin:0 0 12px;font:600 18px 'Segoe UI',Arial,sans-serif;color:#1e3a8a;\">{escape(heading)}</h2>",
    ]
    if events:
        text_parts.extend(["Assignment history", ""])
        html_parts.append(
            "<p style=\"margin:0 0 8px;font:600 14px 'Segoe UI',Arial,sans-serif;color:#334155;\">Assignment history</p>"
            "<ol style=\"margin:0 0 16px 18px;padding:0;font:14px 'Segoe UI',Arial,sans-serif;color:#0f172a;\">"
        )
        for event in events:
            text_parts.append(event.line)
            html_parts.append(f"<li style=\"margin:0 0 6px 0;\">{escape(event.line)}</li>")
        text_parts.append("")
        html_parts.append("</ol>")
    text_parts.extend(["Conversation", ""])
    html_parts.append(
        "<p style=\"margin:0 0 16px;font:14px 'Segoe UI',Arial,sans-serif;color:#334155;\">Conversation</p>"
    )
    attachments: list[tuple[str, bytes, str]] = []
    used_names: set[str] = set()
    used_bytes = 0
    omitted: list[str] = []

    for comment in comments:
        when = date_format(comment.created_at, "d M Y, H:i")
        who = comment.user_email or "system"
        if comment.outbound:
            who = f"{who} (sent)"
        text_parts.append(f"{who} — {when}")
        html_parts.append(
            "<div style=\"margin:0 0 14px;padding:10px 12px;border:1px solid #dbeafe;border-radius:8px;\">"
            f"<div style=\"font:600 13px 'Segoe UI',Arial,sans-serif;color:#1e3a8a;\">{escape(who)}"
            f" <span style=\"font-weight:400;color:#64748b;\">{escape(when)}</span></div>"
        )
        if comment.body:
            text_parts.append(comment.body)
            html_parts.append(
                "<div style=\"margin-top:6px;font:14px 'Segoe UI',Arial,sans-serif;color:#0f172a;white-space:normal;\">"
                f"{_html_lines(comment.body)}</div>"
            )
        file_names = []
        for raw_name, content, mime in comment.files:
            safe_name = _unique_filename(raw_name, used_names)
            if used_bytes + len(content) > _MAX_ATTACHMENT_BYTES:
                omitted.append(safe_name)
                continue
            attachments.append((safe_name, content, mime))
            used_bytes += len(content)
            file_names.append(safe_name)
        if file_names:
            joined = ", ".join(file_names)
            text_parts.append(f"Files: {joined}")
            html_parts.append(
                "<div style=\"margin-top:6px;font:13px 'Segoe UI',Arial,sans-serif;color:#475569;\">"
                f"Files: {escape(joined)}</div>"
            )
        text_parts.append("")
        html_parts.append("</div>")

    if omitted:
        note = "Some files were left out because the email would be too large: " + ", ".join(omitted)
        text_parts.extend(["", note])
        html_parts.append(
            f"<p style=\"font:13px 'Segoe UI',Arial,sans-serif;color:#b45309;\">{escape(note)}</p>"
        )

    if not comments:
        text_parts.append("No conversation yet.")
        html_parts.append(
            "<p style=\"font:14px 'Segoe UI',Arial,sans-serif;color:#64748b;\">No conversation yet.</p>"
        )

    return (
        "\n".join(text_parts).strip(),
        "".join(html_parts),
        attachments,
        omitted,
        bool(comments) or bool(events),
    )


def send_ticket_conversation_summary(*, company, ticket, to_email: str) -> tuple[str, str | None]:
    """
    Send the public conversation.

    Returns (status, error). status is sent, empty, skipped_no_smtp, or failed.
    """
    text_body, html_body, attachments, _omitted, has_messages = build_conversation_summary(
        ticket
    )
    if not has_messages:
        return "empty", "This ticket has no conversation to send."

    creds = _smtp_credentials(company)
    if not creds:
        return (
            "skipped_no_smtp",
            "Configure support/outgoing email and app password in Company profile.",
        )
    from_email, password = creds
    title = (getattr(ticket, "title", None) or "Support ticket").strip()
    subject = f"[{_ticket_ref(ticket)}] Conversation summary — {title}"[:200]
    recipient = (to_email or "").strip()

    from business.outbound_mail import _graph_configured
    from business.support_inbox import _detect_imap_host, _is_microsoft_host

    if _graph_configured() and _is_microsoft_host(_detect_imap_host(from_email)):
        ok, err, _mid = send_via_microsoft_graph(
            from_email,
            [recipient],
            subject,
            html_body,
            file_attachments=attachments,
        )
        if ok:
            logger.info(
                "Ticket conversation summary sent via Graph ticket=%s to=%s",
                ticket.pk,
                recipient,
            )
            return "sent", None
        logger.warning(
            "Graph summary failed for ticket %s, falling back to SMTP: %s",
            ticket.pk,
            err,
        )

    ok, err = send_outbound(
        from_email=from_email,
        app_password=password,
        to_addresses=[recipient],
        subject=subject,
        body=text_body,
        html_body=html_body,
        attachments=attachments,
    )
    if ok:
        logger.info(
            "Ticket conversation summary sent ticket=%s to=%s",
            ticket.pk,
            recipient,
        )
        return "sent", None
    return "failed", err or "Could not send the email."
