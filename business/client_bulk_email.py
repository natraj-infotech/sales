"""Send custom bulk email to selected clients (one message per client)."""

from __future__ import annotations

import logging
from html import escape

from business.oem_tickets import collect_attachments
from business.outbound_mail import send_company_outbound
from business.reminder_schedule import apply_merge_tags

logger = logging.getLogger(__name__)

BULK_EMAIL_MERGE_TAGS = (
    ("client_name", "Client name"),
    ("company_name", "Your company name"),
    ("gst_number", "Client GST number"),
)


def client_bulk_merge_context(company, client) -> dict[str, str]:
    return {
        "client_name": (client.name or "").strip(),
        "company_name": (getattr(company, "company_name", None) or "").strip(),
        "gst_number": (client.gst_number or "").strip(),
    }


def preview_client_email(company, client, subject_template: str, body_template: str) -> dict:
    context = client_bulk_merge_context(company, client)
    return {
        "client_name": client.name or "",
        "to_emails": client.contact_emails(),
        "subject": apply_merge_tags(subject_template, context).strip(),
        "body": apply_merge_tags(body_template, context).strip(),
    }


def _plain_to_html(text: str) -> str:
    lines = (text or "").splitlines()
    if not lines:
        return "<br>"
    return "<br>\n".join(escape(line) for line in lines)


def send_client_bulk_emails(
    company,
    clients,
    subject_template: str,
    body_template: str,
    attachments=None,
) -> tuple[int, list[str], list[str]]:
    """
    Send one outbound message per client (all contact emails in To).

    Returns (sent_client_count, skipped_messages, failure_messages).
    """
    attachment_list = list(attachments or ())
    sent = 0
    skipped: list[str] = []
    failures: list[str] = []

    for client in clients:
        to_addresses = client.contact_emails()
        if not to_addresses:
            skipped.append(f"{client.name}: no email address on file.")
            continue

        context = client_bulk_merge_context(company, client)
        subject = apply_merge_tags(subject_template, context).strip()
        body = apply_merge_tags(body_template, context).strip()
        if not subject:
            failures.append(f"{client.name}: subject is empty after merge tags.")
            continue

        html_body = (
            '<div style="font-family:\'Segoe UI\',Arial,Helvetica,sans-serif;'
            'font-size:14px;line-height:1.55;color:#334155;">'
            f"{_plain_to_html(body)}</div>"
        )
        ok, err = send_company_outbound(
            company,
            to_addresses,
            subject,
            body,
            html_body=html_body,
            attachments=attachment_list or None,
        )
        if ok:
            sent += 1
            logger.info(
                "Bulk client email sent company_id=%s client_id=%s to=%s",
                company.pk,
                client.pk,
                to_addresses,
            )
        else:
            failures.append(f"{client.name}: {err or 'send failed'}.")

    return sent, skipped, failures


__all__ = [
    "BULK_EMAIL_MERGE_TAGS",
    "client_bulk_merge_context",
    "collect_attachments",
    "send_client_bulk_emails",
]
