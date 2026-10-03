"""Ask vendors for a price on one subscription, integrating with Procurement deals."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from html import escape
import logging
import threading

from django.conf import settings
from django.db import close_old_connections, transaction
from django.urls import reverse
from django.utils import timezone

from business.expiry_notifications import (
    _company_logo_inline,
    _days_left_badge,
    _subscription_bill_type,
)
from business.outbound_mail import (
    open_smtp_connection,
    send_smtp_outbound,
    send_via_microsoft_graph,
)
from business.renewal_read_tracking import _graph_configured
from business.support_inbox import _detect_imap_host, _is_microsoft_host

logger = logging.getLogger(__name__)

VENDOR_PRICE_CTA = "SUBMIT PRICE"
VENDOR_PRICE_OFFER = (
    "Please share your best price for the product and quantity listed below."
)


def _public_base_url() -> str:
    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if base:
        return base
    return "http://127.0.0.1:8000"


def vendor_quote_absolute_url(token) -> str:
    path = reverse("vendor_quote_public", kwargs={"token": str(token)})
    return f"{_public_base_url()}{path}"


def next_subscription_deal_no(company, when=None) -> str:
    from business.models import ProcurementDeal

    now = when or timezone.localtime()
    prefix = f"SQ-{now.strftime('%d%m%Y')}"
    existing = (
        ProcurementDeal.objects.filter(company=company, deal_no__startswith=prefix)
        .values_list("deal_no", flat=True)
        .distinct()
    )
    max_seq = 0
    for deal_no in existing:
        suffix = str(deal_no)[len(prefix) :]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return f"{prefix}{max_seq + 1:04d}"


def vendor_price_email(company, subscription, quote_url: str, deal_no: str = ""):
    product_name = subscription.product.name
    qty = subscription.quantity
    company_name = company.company_name or "Company"
    client_name = (getattr(subscription.client, "name", "") or "").strip() or "—"
    subscription_id = subscription.subscription_list_id
    bill_type = _subscription_bill_type(subscription)
    end_date = (
        subscription.end_date.strftime("%d %b %Y") if subscription.end_date else "—"
    )
    days_label, _, days_accent = _days_left_badge(subscription.days_until_expiry)

    deal_suffix = f" [Deal No: {deal_no}]" if deal_no else ""
    subject = f"Price quotation request - {product_name} (qty {qty}){deal_suffix}"

    body_lines = [
        "Dear Sir/Madam,",
        "",
        f"Please share your best price for the product and quantity listed below for {company_name}:",
        "",
        f"Client: {client_name}",
        f"Product: {product_name}",
        f"Subscription ID: {subscription_id}",
        f"Bill Type: {bill_type}",
        f"Quantity: {qty}",
        f"End Date: {end_date}",
        f"Days Left: {days_label}",
    ]
    if deal_no:
        body_lines.append(f"Deal Number: {deal_no}")
    body_lines.extend(
        [
            "",
            "Please submit your price using the link below:",
            f"{quote_url}",
            "",
            "Best regards,",
            company_name,
        ]
    )
    body = "\n".join(body_lines)

    logo = _company_logo_inline(company)
    logo_cid = logo[0] if logo else ""

    if logo_cid:
        logo_tag = (
            f'<img src="cid:{escape(logo_cid)}" alt="{escape(company_name)}" '
            'style="max-height:45px;max-width:180px;border:0;display:block;">'
        )
    else:
        logo_tag = f'<h2 style="margin:0;color:#1e3a8a;font-size:20px;font-weight:700;">{escape(company_name)}</h2>'

    deal_badge = ""
    if deal_no:
        deal_badge = (
            f'<span style="display:inline-block;padding:3px 10px;background:#dbeafe;'
            f'color:#1e3a8a;border-radius:999px;font-size:12px;font-weight:700;margin-top:6px;">'
            f"Deal No: {escape(deal_no)}</span>"
        )

    html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{ font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, Helvetica, Arial, sans-serif; background-color: #f1f5f9; margin: 0; padding: 24px; color: #1e293b; }}
.card {{ max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); border: 1px solid #e2e8f0; }}
.header {{ padding: 22px 28px 18px; border-bottom: 1px solid #e2e8f0; background: #ffffff; }}
.content {{ padding: 28px; }}
.table {{ width: 100%; border-collapse: collapse; margin: 20px 0; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; }}
.table th {{ background: #f8fafc; color: #475569; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 700; padding: 10px 14px; border-bottom: 1px solid #e2e8f0; text-align: left; }}
.table td {{ padding: 12px 14px; border-bottom: 1px solid #f1f5f9; font-size: 14px; color: #0f172a; }}
.btn {{ display: inline-block; padding: 11px 26px; background-color: #2563eb; color: #ffffff !important; text-decoration: none; border-radius: 7px; font-weight: 700; font-size: 14px; letter-spacing: 0.03em; text-transform: uppercase; margin-top: 10px; }}
.footer {{ padding: 16px 28px; background: #f8fafc; border-top: 1px solid #e2e8f0; font-size: 12px; color: #64748b; line-height: 1.5; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    {logo_tag}
    {deal_badge}
  </div>
  <div class="content">
    <p style="margin: 0 0 12px; font-size: 15px; font-weight: 600; color: #0f172a;">Dear Partner,</p>
    <p style="margin: 0 0 16px; font-size: 14px; line-height: 1.5; color: #334155;">
      {VENDOR_PRICE_OFFER}
    </p>
    <table class="table">
      <thead>
        <tr>
          <th>Client</th>
          <th>Product Description</th>
          <th>Subscription ID</th>
          <th style="text-align: center;">Qty</th>
          <th style="text-align: center;">End Date</th>
          <th style="text-align: center;">Days Left</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td style="vertical-align: top;">{escape(client_name)}</td>
          <td style="vertical-align: top;">
            <strong>{escape(product_name)}</strong><br>
            <span style="color: #64748b; font-size: 12px;">Bill Type: {escape(bill_type)}</span>
          </td>
          <td style="vertical-align: top; word-break: break-all; font-size: 12px;">{escape(subscription_id)}</td>
          <td style="text-align: center; font-weight: 700;">{qty}</td>
          <td style="text-align: center; white-space: nowrap;">{escape(end_date)}</td>
          <td style="text-align: center;">
            <span style="display: inline-block; padding: 4px 9px; border: 1px solid {days_accent}; border-radius: 999px; color: {days_accent}; white-space: nowrap; font-size: 12px;">{escape(days_label)}</span>
          </td>
        </tr>
      </tbody>
    </table>
    <div style="margin: 24px 0 10px; text-align: center;">
      <a href="{escape(quote_url)}" class="btn" target="_blank">{VENDOR_PRICE_CTA}</a>
    </div>
  </div>
  <div class="footer">
    This quotation request was sent by <strong>{escape(company_name)}</strong>. Please click the button above to submit your price quotation directly.
  </div>
</div>
</body>
</html>"""
    inline_images = [logo] if logo else None
    return subject, body, html_body, inline_images


def _uses_graph(sender: str) -> bool:
    return _is_microsoft_host(_detect_imap_host(sender)) and _graph_configured()


def send_vendor_price_email(
    company,
    to_addresses: list[str],
    subject: str,
    body: str,
    html_body: str,
    inline_images=None,
    connection=None,
) -> tuple[bool, str]:
    sender = (company.outgoing_email or "").strip()
    password = (company.outgoing_email_app_password or "").strip()
    if not sender or not password:
        return False, "Configure outgoing SMTP email and app password in Company profile."

    if _uses_graph(sender):
        ok, err, _message_id = send_via_microsoft_graph(
            sender,
            to_addresses,
            subject,
            html_body,
            inline_images=inline_images,
        )
        if ok:
            return True, ""
        logger.warning(
            "Graph vendor price send failed for %s, falling back to SMTP: %s",
            sender,
            err,
        )

    ok, err = send_smtp_outbound(
        sender,
        password,
        to_addresses,
        subject,
        body,
        html_body=html_body,
        inline_images=inline_images,
        connection=connection,
    )
    return ok, err or ""


def _start_background(fn, *args) -> None:
    threading.Thread(
        target=fn, args=args, name="vendor-price-emails", daemon=True
    ).start()


def _send_price_emails(company_id: int, jobs: list[dict]) -> None:
    """Send queued vendor price mails over one SMTP login; runs off the request thread."""
    from accounts.models import CompanyProfile
    from business.models import VendorPriceRequest
    from business.team_notifications import notify_email_failed

    close_old_connections()
    connection = None
    try:
        company = CompanyProfile.objects.get(pk=company_id)
        sender = (company.outgoing_email or "").strip()
        if sender and not _uses_graph(sender):
            connection = open_smtp_connection(
                sender, company.outgoing_email_app_password or ""
            )
        failed = []
        for job in jobs:
            ok, err = send_vendor_price_email(
                company,
                job["addresses"],
                job["subject"],
                job["body"],
                job["html_body"],
                inline_images=job["inline_images"],
                connection=connection,
            )
            if ok:
                VendorPriceRequest.objects.filter(pk=job["request_id"]).update(
                    email_sent_at=timezone.now()
                )
                continue
            VendorPriceRequest.objects.filter(pk=job["request_id"]).delete()
            failed.append(f"{job['vendor_name']}: {err or 'send failed'}")
            if connection is not None:
                connection.close()
                connection = None
        if failed:
            logger.warning(
                "Vendor price request mail failed company_id=%s: %s",
                company_id,
                "; ".join(failed),
            )
            notify_email_failed(
                company,
                area="vendor_price_request",
                title="Vendor price request email failed",
                body="; ".join(failed),
            )
    except Exception:
        logger.exception("Vendor price request mail crashed company_id=%s", company_id)
    finally:
        if connection is not None:
            connection.close()
        close_old_connections()


def send_subscription_price_requests(company, subscription, vendors) -> dict:
    """Queue a price-form email per vendor for this subscription, and create a ProcurementDeal.

    Emails go out in a background thread after the transaction commits, so the
    request returns right away. `queued` lists vendors whose email is on its way.
    """
    from business.models import (
        DealRound,
        DealVendor,
        ProcurementDeal,
        VendorPriceRequest,
        VendorPriceSubmission,
    )

    if not (
        (company.outgoing_email or "").strip()
        and (company.outgoing_email_app_password or "").strip()
    ):
        return {
            "queued": [],
            "errors": [
                "Configure outgoing SMTP email and app password in Company profile."
            ],
            "deal_no": "",
        }

    # Find or create a ProcurementDeal for this subscription
    existing_deal = (
        ProcurementDeal.objects.filter(
            company=company, deal_name=f"Subscription:{subscription.pk}"
        )
        .order_by("-id")
        .first()
    )
    if existing_deal:
        deal_no = existing_deal.deal_no
        deal = existing_deal
    else:
        deal_no = next_subscription_deal_no(company)
        deal = ProcurementDeal.objects.create(
            company=company,
            deal_no=deal_no,
            deal_name=f"Subscription:{subscription.pk}",
            category=subscription.product.category or "",
            product=subscription.product,
            product_company=getattr(subscription.product, "company_name", "") or "",
            quantity=subscription.quantity,
            price=Decimal("0.00"),
        )

    round_obj, _ = DealRound.objects.get_or_create(
        company=company,
        deal_no=deal_no,
        round_number=1,
        defaults={"name": "Round 1"},
    )

    queued = []
    errors = []
    jobs = []
    for vendor in vendors:
        addresses = vendor.contact_emails()
        if not addresses:
            errors.append(f"{vendor.name} has no email.")
            continue

        DealVendor.objects.get_or_create(
            company=company,
            deal_no=deal_no,
            vendor=vendor,
            defaults={"deal": deal, "price": Decimal("0.00")},
        )
        VendorPriceSubmission.objects.get_or_create(
            company=company,
            deal_no=deal_no,
            vendor=vendor,
            deal_round=round_obj,
            defaults={
                "deal": deal,
                "expires_at": timezone.now() + timedelta(days=7),
            },
        )

        request_row = VendorPriceRequest.objects.create(
            company=company,
            subscription=subscription,
            vendor=vendor,
            product_name=subscription.product.name,
            quantity=subscription.quantity,
            client_name=(subscription.client.name or "").strip(),
            end_date=None,
        )
        quote_url = vendor_quote_absolute_url(request_row.token)
        subject, body, html_body, inline_images = vendor_price_email(
            company, subscription, quote_url, deal_no=deal_no
        )
        jobs.append(
            {
                "request_id": request_row.pk,
                "vendor_name": vendor.name,
                "addresses": addresses,
                "subject": subject,
                "body": body,
                "html_body": html_body,
                "inline_images": inline_images,
            }
        )
        queued.append(vendor.name)

    if jobs:
        company_id = company.pk
        transaction.on_commit(
            lambda: _start_background(_send_price_emails, company_id, jobs)
        )
    return {"queued": queued, "errors": errors, "deal_no": deal_no}
