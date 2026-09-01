"""Poll the company Case lock mailbox and email one-time form links to senders."""

from __future__ import annotations

import email.policy
import imaplib
import logging
from email import message_from_bytes
from email.utils import parsedate_to_datetime
from html import escape as html_escape

import requests
from django.conf import settings
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import CompanyProfile
from business.models import CaseLockInvite, CaseLockSubmission
from business.outbound_mail import send_outbound
from business.support_inbox import (
    _decode_header,
    _detect_imap_host,
    _fetch_rfc822_bytes,
    _graph_access_token,
    _graph_list_unread_inbox,
    _graph_mark_read,
    _is_microsoft_host,
    parse_rfc5322_addr,
    stable_fallback_id,
)

logger = logging.getLogger(__name__)


def _public_base_url() -> str:
    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if base:
        return base
    return "http://127.0.0.1:8000"


def invite_form_absolute_url(invite: CaseLockInvite) -> str:
    path = reverse("caselock_public_form", kwargs={"invite_id": str(invite.pk)})
    return f"{_public_base_url()}{path}"


def _aware(dt):
    if dt is None:
        return timezone.now()
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


_BOUNCE_SUBJECT_MARKERS = (
    "undeliverable",
    "delivery status notification",
    "mail delivery failed",
    "undelivered mail",
    "returned mail",
    "failure notice",
    "automatic reply",
    "auto-reply",
    "autoreply",
    "out of office",
)


def _send_mail_via_company(
    company: CompanyProfile,
    to_email: str,
    subject: str,
    body: str,
    html_body: str,
    inline_images=None,
) -> bool:
    mailbox = (company.outgoing_email or "").strip()
    password = company.outgoing_email_app_password or ""
    if _looks_like_bounce_or_auto(to_email, subject):
        logger.info(
            "Case lock outbound skipped: bounce/system recipient %s (company %s)",
            (to_email or "").strip()[:80],
            company.pk,
        )
        return False
    if not mailbox:
        logger.info(
            "Case lock outbound skipped: company %s has no outgoing_email.",
            company.pk,
        )
        return False
    ok, _err = send_outbound(
        mailbox,
        password,
        [to_email],
        subject,
        body,
        html_body=html_body,
        inline_images=inline_images,
    )
    return ok


def _caselock_logo_html(company: CompanyProfile, logo_cid: str, header_bg: str, font: str) -> str:
    company_name = html_escape(company.company_name)
    if logo_cid:
        return (
            f'<img src="cid:{html_escape(logo_cid)}" alt="{company_name}" width="120" '
            'style="display:block;max-width:140px;max-height:48px;width:auto;height:auto;'
            'object-fit:contain;background-color:#FFFFFF;border-radius:4px;padding:2px;">'
        )
    initial = html_escape((company.company_name or "C")[:1].upper())
    return (
        '<div style="width:44px;height:44px;border-radius:4px;background-color:#FFFFFF;'
        f'color:{header_bg};font-family:{font};font-size:18px;font-weight:700;'
        'line-height:44px;text-align:center;">'
        f"{initial}</div>"
    )


def _caselock_contact_html(company: CompanyProfile) -> str:
    from business.expiry_notifications import _client_contact_lines

    bits = _client_contact_lines(company)
    if not bits:
        return ""
    return "<br>\n              " + "<br>\n              ".join(html_escape(bit) for bit in bits)


def _caselock_branded_html(
    company: CompanyProfile,
    *,
    kicker: str,
    title: str,
    intro_html: str,
    extra_html: str = "",
    cta_url: str = "",
    cta_label: str = "",
    logo_cid: str = "",
    footer_note: str = "",
    title_color: str | None = None,
) -> str:
    font = "'Segoe UI', Arial, Helvetica, sans-serif"
    text = "#374151"
    muted = "#6B7280"
    blue = "#2563EB"
    header_bg = "#3B5998"
    border = "#E5E7EB"
    page_bg = "#F3F4F6"
    card_bg = "#FFFFFF"
    heading = title_color or blue
    company_name = html_escape(company.company_name)
    logo_html = _caselock_logo_html(company, logo_cid, header_bg, font)
    contact_html = _caselock_contact_html(company)
    cta_html = ""
    if cta_url and cta_label:
        cta_html = """
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 20px 0;">
                <tr>
                  <td align="center" bgcolor="{header_bg}" style="background-color:{header_bg};border-radius:4px;">
                    <a href="{cta_url}"
                       style="display:inline-block;padding:12px 22px;font-family:{font};font-size:14px;font-weight:600;color:#FFFFFF;text-decoration:none;">
                      {cta_label}
                    </a>
                  </td>
                </tr>
              </table>
""".format(
            header_bg=header_bg,
            font=font,
            cta_url=html_escape(cta_url, quote=True),
            cta_label=html_escape(cta_label),
        )
    footer_html = ""
    if footer_note:
        footer_html = (
            f'<p style="margin:20px 0 0 0;padding-top:16px;border-top:1px solid {border};'
            f'font-size:12px;line-height:1.5;color:{muted};">{html_escape(footer_note)}</p>'
        )
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0;padding:0;background-color:{page_bg};">
  <tr>
    <td align="center" style="padding:24px 12px;background-color:{page_bg};">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background-color:{card_bg};border:1px solid {border};border-radius:8px;">
        <tr>
          <td bgcolor="{header_bg}" style="background-color:{header_bg};padding:14px 18px;border-radius:8px 8px 0 0;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td align="left" valign="middle">
                  {logo_html}
                </td>
                <td align="right" valign="middle" style="font-family:{font};font-size:18px;font-weight:700;color:#FFFFFF;padding-left:12px;">
                  {company_name}
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:28px 28px 8px 28px;font-family:{font};">
            <p style="margin:0 0 6px 0;font-size:12px;letter-spacing:0.4px;text-transform:uppercase;color:{blue};font-weight:600;">
              {html_escape(kicker)}
            </p>
            <h1 style="margin:0 0 16px 0;font-size:22px;line-height:1.3;color:{heading};font-weight:700;font-family:{font};">
              {html_escape(title)}
            </h1>
            {intro_html}
            {cta_html}
            {extra_html}
            <p style="margin:16px 0 0 0;font-size:15px;line-height:1.55;color:{text};">
              Best Regards,<br>
              <strong style="color:{blue};">{company_name}</strong>{contact_html}
            </p>
            {footer_html}
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
""".strip()


def _caselock_details_table(rows: list[tuple[str, str]]) -> str:
    font = "'Segoe UI', Arial, Helvetica, sans-serif"
    text = "#374151"
    border = "#E5E7EB"
    card_bg = "#FFFFFF"
    body = []
    for label, value in rows:
        body.append(
            f"""
                <tr>
                  <td style="padding:12px;border:1px solid {border};background-color:{card_bg};color:{text};font-family:{font};font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.3px;width:40%;">
                    {html_escape(label)}
                  </td>
                  <td style="padding:12px;border:1px solid {border};color:{text};font-family:{font};font-size:14px;background-color:{card_bg};">
                    {html_escape(value)}
                  </td>
                </tr>
            """.strip()
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse;width:100%;margin:0 0 16px 0;">'
        + "".join(body)
        + "</table>"
    )


def _caselock_mail_assets(company: CompanyProfile):
    from business.expiry_notifications import _company_logo_inline

    logo = _company_logo_inline(company)
    logo_cid = logo[0] if logo else ""
    images = [logo] if logo else None
    return logo_cid, images


def _sender_local_part(addr: str) -> str:
    value = (addr or "").strip().lower()
    if "@" in value:
        return value.split("@", 1)[0]
    return value


def _looks_like_bounce_or_auto(sender: str, subject: str) -> bool:
    subj = (subject or "").strip().lower()
    if any(marker in subj for marker in _BOUNCE_SUBJECT_MARKERS):
        return True
    local = _sender_local_part(sender)
    if local.startswith("microsoftexchange"):
        return True
    if local in {"postmaster", "mailer-daemon", "mailerdaemon"}:
        return True
    return False


def _is_company_own_mailbox(company: CompanyProfile, sender: str) -> bool:
    addr = (sender or "").strip().lower()
    if not addr:
        return True
    own = {
        (company.caselock_email or "").strip().lower(),
        (company.outgoing_email or "").strip().lower(),
    }
    own.discard("")
    return addr in own


def _should_skip_inbound(company: CompanyProfile, sender: str, subject: str) -> bool:
    if not (sender or "").strip():
        return True
    if _is_company_own_mailbox(company, sender):
        return True
    return _looks_like_bounce_or_auto(sender, subject)


def _should_skip_message(company: CompanyProfile, msg) -> bool:
    auto = (msg.get("Auto-Submitted") or "").lower()
    if auto and auto not in ("no", ""):
        return True
    prec = (msg.get("Precedence") or "").lower()
    if prec in ("bulk", "junk", "list"):
        return True
    from_decoded = _decode_header(msg.get("From", ""))
    sender = parse_rfc5322_addr(from_decoded) or (from_decoded or "")[:254]
    subject = _decode_header(msg.get("Subject", "")) or ""
    return _should_skip_inbound(company, sender, subject)


def _send_invite_email(company: CompanyProfile, invite: CaseLockInvite, sender: str) -> None:
    url = invite_form_absolute_url(invite)
    body = (
        "Thank you for contacting us.\n\n"
        f"Please submit your Case lock details using this secure link:\n{url}\n\n"
        "If you did not send that message, you can ignore this email.\n"
    )
    logo_cid, images = _caselock_mail_assets(company)
    intro = """
            <p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:#374151;font-family:'Segoe UI', Arial, Helvetica, sans-serif;">
              Dear Sir/Madam,
            </p>
            <p style="margin:0 0 16px 0;font-size:15px;line-height:1.55;color:#374151;font-family:'Segoe UI', Arial, Helvetica, sans-serif;">
              Thank you for contacting us. Please submit your Case lock details using the button below.
            </p>
"""
    html_body = _caselock_branded_html(
        company,
        kicker="Case lock",
        title="Submit your Case lock details",
        intro_html=intro,
        cta_url=url,
        cta_label="SUBMIT CASE LOCK DETAILS",
        logo_cid=logo_cid,
        footer_note="If you did not send that message, you can ignore this email.",
    )
    subj = "Case lock details"
    ok = _send_mail_via_company(
        company, sender, subj, body, html_body, inline_images=images
    )
    invite.email_sent_at = timezone.now() if ok else None
    invite.save(update_fields=["email_sent_at"])
    if not ok:
        logger.info(
            "Case lock invite %s — outbound mail failed for mailbox %s; link: %s",
            invite.pk,
            company.outgoing_email,
            url,
        )


def send_caselock_decision_email(submission: CaseLockSubmission) -> bool:
    invite = submission.invite
    company = invite.company
    if company is None and invite.inbox_id:
        company = invite.inbox.company
    to_email = (invite.sender_email or "").strip()
    if not to_email or company is None:
        return False
    logo_cid, images = _caselock_mail_assets(company)
    product = submission.product_name or "—"
    customer = submission.client_name or "—"
    qty = str(submission.product_quantity)
    if submission.status == CaseLockSubmission.Status.APPROVED:
        subject = f"Final case lock — {submission.product_name}"
        body = (
            f"Your case lock request for product '{submission.product_name}' "
            "has been finalized.\n\n"
            f"Customer: {submission.client_name}\n"
            f"GST: {submission.client_gst or '—'}\n"
            f"Quantity: {submission.product_quantity}\n"
        )
        intro = """
            <p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:#374151;font-family:'Segoe UI', Arial, Helvetica, sans-serif;">
              Dear Sir/Madam,
            </p>
            <p style="margin:0 0 16px 0;font-size:15px;line-height:1.55;color:#374151;font-family:'Segoe UI', Arial, Helvetica, sans-serif;">
              Your case lock request has been <strong>finalized</strong>.
            </p>
"""
        extra = _caselock_details_table(
            [
                ("Product", product),
                ("Customer", customer),
                ("GST", submission.client_gst or "—"),
                ("Quantity", qty),
            ]
        )
        html_body = _caselock_branded_html(
            company,
            kicker="Case lock confirmation",
            title="Case lock request finalized",
            intro_html=intro,
            extra_html=extra,
            logo_cid=logo_cid,
        )
    elif submission.status == CaseLockSubmission.Status.DECLINED:
        subject = f"Case lock declined — {submission.product_name}"
        body = (
            f"Your case lock request for product '{submission.product_name}' "
            "has been declined.\n\n"
            f"Customer: {submission.client_name}\n"
            f"Quantity: {submission.product_quantity}\n"
        )
        intro = """
            <p style="margin:0 0 12px 0;font-size:15px;line-height:1.55;color:#374151;font-family:'Segoe UI', Arial, Helvetica, sans-serif;">
              Dear Sir/Madam,
            </p>
            <p style="margin:0 0 16px 0;font-size:15px;line-height:1.55;color:#374151;font-family:'Segoe UI', Arial, Helvetica, sans-serif;">
              Your case lock request has been <strong>declined</strong>.
            </p>
"""
        extra = _caselock_details_table(
            [
                ("Product", product),
                ("Customer", customer),
                ("Quantity", qty),
            ]
        )
        html_body = _caselock_branded_html(
            company,
            kicker="Case lock update",
            title="Case lock request declined",
            intro_html=intro,
            extra_html=extra,
            logo_cid=logo_cid,
            title_color="#B91C1C",
        )
    else:
        return False
    return _send_mail_via_company(
        company, to_email, subject, body, html_body, inline_images=images
    )


def _create_invite_and_notify(
    company: CompanyProfile,
    mid: str,
    sender: str,
    subject: str,
    received_at=None,
) -> bool:
    if _should_skip_inbound(company, sender, subject):
        logger.info(
            "Case lock: skip inbound from %s subject %r (company %s)",
            (sender or "").strip()[:80],
            (subject or "")[:80],
            company.pk,
        )
        return False
    sender_clean = sender.strip()[:254]
    try:
        invite = CaseLockInvite.objects.create(
            company=company,
            source_message_id=mid,
            sender_email=sender_clean,
            mail_subject=(subject or "")[:255],
            received_at=_aware(received_at),
        )
    except IntegrityError:
        return False
    _send_invite_email(company, invite, sender_clean)
    return True


def _invite_exists(company: CompanyProfile, mid: str) -> bool:
    return CaseLockInvite.objects.filter(company=company, source_message_id=mid).exists()


def _fetch_caselock_via_graph(company: CompanyProfile, mark_seen: bool) -> tuple[int, list[str]]:
    log: list[str] = []
    created = 0
    mailbox = (company.caselock_email or "").strip()
    access_token = _graph_access_token()
    items = _graph_list_unread_inbox(
        access_token,
        mailbox,
        select="id,subject,from,internetMessageId,bodyPreview,receivedDateTime",
        unread_only=True,
    )

    for item in items:
        mid = (item.get("internetMessageId") or "").strip()
        gid = (item.get("id") or "").strip()
        if not mid:
            mid = f"graph:{gid}"
        if not mid:
            continue

        if _invite_exists(company, mid):
            if mark_seen and gid:
                try:
                    _graph_mark_read(access_token, mailbox, gid)
                except Exception:
                    pass
            continue

        subject = (item.get("subject") or "(no subject)").strip()[:255]
        from_obj = item.get("from") or {}
        sender = (
            ((from_obj.get("emailAddress") or {}).get("address") or "").strip()
        )
        received_raw = item.get("receivedDateTime") or ""
        received_at = parse_datetime(received_raw) if received_raw else None
        if _should_skip_inbound(company, sender, subject):
            if mark_seen and gid:
                try:
                    _graph_mark_read(access_token, mailbox, gid)
                except Exception:
                    pass
            continue

        if _create_invite_and_notify(company, mid, sender, subject, received_at=received_at):
            created += 1

        if mark_seen and gid:
            try:
                _graph_mark_read(access_token, mailbox, gid)
            except Exception:
                pass

    log.append(
        f"[CaseLock {mailbox}] Microsoft Graph: {created} new invite(s) "
        f"from {len(items)} unread."
    )
    return created, log


def fetch_caselock_for_company(company: CompanyProfile) -> tuple[int, list[str]]:
    logs: list[str] = []
    mailbox = (company.caselock_email or "").strip()
    password = company.caselock_email_app_password or ""
    if not mailbox:
        return 0, logs

    created_total = 0
    mark_seen = getattr(settings, "SUPPORT_IMAP_MARK_SEEN", True)
    host = _detect_imap_host(mailbox)
    port = 993
    mailbox_name = getattr(settings, "SUPPORT_IMAP_MAILBOX", "INBOX")

    if _is_microsoft_host(host):
        try:
            c, l = _fetch_caselock_via_graph(company, mark_seen=mark_seen)
            return c, l
        except Exception as e:
            logs.append(f"[CaseLock] Graph error ({mailbox}): {e}")
            if not password.strip():
                return 0, logs

    if not password.strip():
        logs.append(
            f"[CaseLock] {mailbox}: add a mailbox app password for IMAP, "
            "or use Microsoft 365 with Graph app credentials in settings."
        )
        return 0, logs

    imap: imaplib.IMAP4_SSL | None = None
    try:
        imap = imaplib.IMAP4_SSL(host, port, timeout=60)
        imap.login(mailbox, password)
        typ, _ = imap.select(mailbox_name)
        if typ != "OK":
            logs.append(
                f"[CaseLock] could not select mailbox {mailbox_name!r} for {mailbox}."
            )
            return 0, logs

        typ, data = imap.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return 0, logs

        ids = data[0].split()
        for num in ids:
            raw = _fetch_rfc822_bytes(imap, num)
            if not raw:
                continue
            msg = message_from_bytes(raw, policy=email.policy.default)
            if _should_skip_message(company, msg):
                if mark_seen:
                    imap.store(num, "+FLAGS", "\\Seen")
                continue

            mid = (msg.get("Message-ID") or "").strip()
            if not mid:
                mid = stable_fallback_id(raw)

            if _invite_exists(company, mid):
                if mark_seen:
                    imap.store(num, "+FLAGS", "\\Seen")
                continue

            subject = _decode_header(msg.get("Subject", "")) or "(no subject)"
            subject = subject.strip()[:255]
            from_decoded = _decode_header(msg.get("From", ""))
            sender = parse_rfc5322_addr(from_decoded) or from_decoded[:254]
            received_at = None
            try:
                date_hdr = msg.get("Date")
                if date_hdr:
                    received_at = parsedate_to_datetime(str(date_hdr))
            except (TypeError, ValueError, OverflowError):
                received_at = None

            if _create_invite_and_notify(
                company, mid, sender, subject, received_at=received_at
            ):
                created_total += 1

            if mark_seen:
                imap.store(num, "+FLAGS", "\\Seen")

        logs.append(
            f"[CaseLock {mailbox}] IMAP: {created_total} invite(s) "
            f"from {len(ids)} unread message(s)."
        )
    except imaplib.IMAP4.error as e:
        logs.append(f"[CaseLock] IMAP error ({mailbox}): {e}")
    except OSError as e:
        logs.append(f"[CaseLock] connection error ({mailbox}): {e}")
        logger.exception("Case lock IMAP connection error")
    finally:
        if imap is not None:
            try:
                imap.close()
            except Exception:
                pass
            try:
                imap.logout()
            except Exception:
                pass

    return created_total, logs


def should_poll_mailbox_on_request() -> bool:
    """IMAP/Graph from a page or live poll only locally. Production uses Celery beat."""
    import sys

    from django.conf import settings

    if getattr(settings, "DEBUG", False):
        return True
    return any(arg == "runserver" or str(arg).endswith("runserver") for arg in sys.argv)


def poll_error_logs(result: dict | None) -> list[str]:
    if not result:
        return []
    errors: list[str] = []
    for line in result.get("logs") or []:
        lower = str(line).lower()
        if "fail" in lower or "error" in lower:
            errors.append(str(line))
    return errors


def maybe_fetch_on_dashboard(company: CompanyProfile, *, min_interval_seconds: int = 20) -> dict | None:
    from django.core.cache import cache

    key = f"caselock_inbox_poll:{company.pk}"
    if cache.get(key):
        return None
    cache.set(key, 1, timeout=max(5, int(min_interval_seconds)))
    try:
        created, logs = fetch_caselock_for_company(company)
        return {"created": created, "logs": logs}
    except Exception:
        logger.exception("Dashboard Case lock poll failed for company %s", company.pk)
        return {"created": 0, "logs": ["poll failed"]}


def fetch_all_caselock_inboxes() -> dict:
    created_total = 0
    logs: list[str] = []
    qs = CompanyProfile.objects.exclude(caselock_email="").order_by("pk")
    for company in qs:
        c, batch_logs = fetch_caselock_for_company(company)
        created_total += c
        logs.extend(batch_logs)
    return {"created": created_total, "logs": logs[-40:]}
