"""IMAP polling: create SupportTicket rows from unread mail in each company's support mailbox."""

from __future__ import annotations

import hashlib
import imaplib
import logging
import email.policy
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parseaddr
from urllib.parse import quote

import requests
from django.conf import settings
from django.db import IntegrityError
from django.utils.html import strip_tags

from accounts.models import CompanyProfile
from business.models import SupportTicket

logger = logging.getLogger(__name__)

try:
    import dns.resolver  # type: ignore
except Exception:  # pragma: no cover - optional dependency fallback
    dns = None


def _decode_header(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _decode_part_payload(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def extract_plain_body(msg) -> str:
    if msg.is_multipart():
        plain_chunks: list[str] = []
        html_chunks: list[str] = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype == "text/plain":
                plain_chunks.append(_decode_part_payload(part))
            elif ctype == "text/html":
                html_chunks.append(_decode_part_payload(part))
        text = "\n\n".join(t for t in plain_chunks if t.strip())
        if text.strip():
            return text.strip()
        if html_chunks:
            raw = "\n\n".join(html_chunks)
            return strip_tags(raw).replace("\xa0", " ").strip()
        return ""
    ctype = msg.get_content_type()
    if ctype == "text/plain":
        return _decode_part_payload(msg).strip()
    if ctype == "text/html":
        return strip_tags(_decode_part_payload(msg)).replace("\xa0", " ").strip()
    return ""


def parse_rfc5322_addr(from_header: str) -> str:
    _, addr = parseaddr(from_header)
    return (addr or "").strip()


def stable_fallback_id(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _fetch_rfc822_bytes(imap: imaplib.IMAP4_SSL, num: bytes) -> bytes | None:
    typ, msg_data = imap.fetch(num, "(RFC822)")
    if typ != "OK" or not msg_data:
        return None
    for part in msg_data:
        if isinstance(part, tuple) and len(part) >= 2:
            return bytes(part[1])
    return None


def _is_microsoft_host(host: str) -> bool:
    h = (host or "").lower()
    return "outlook.office365.com" in h or "outlook.com" in h or "protection.outlook.com" in h


def _graph_access_token() -> str:
    tenant = getattr(settings, "MICROSOFT_GRAPH_TENANT", "organizations")
    client_id = getattr(settings, "MICROSOFT_GRAPH_CLIENT_ID", "")
    client_secret = getattr(settings, "MICROSOFT_GRAPH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError("Microsoft Graph client credentials are not configured.")
    token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    resp = requests.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    token = payload.get("access_token", "")
    if not token:
        raise RuntimeError("Microsoft Graph token response had no access_token.")
    return token


def _graph_mark_read(access_token: str, mailbox_email: str, message_id: str) -> None:
    url = (
        "https://graph.microsoft.com/v1.0/users/"
        f"{quote(mailbox_email)}/messages/{quote(message_id)}"
    )
    requests.patch(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"isRead": True},
        timeout=30,
    ).raise_for_status()


def _fetch_via_graph(company: CompanyProfile, mark_seen: bool) -> tuple[int, list[str]]:
    created = 0
    log: list[str] = []
    mailbox = company.support_email
    user = company.user
    access_token = _graph_access_token()

    url = (
        "https://graph.microsoft.com/v1.0/users/"
        f"{quote(mailbox)}/mailFolders/Inbox/messages"
    )
    params = {
        "$filter": "isRead eq false",
        "$top": "25",
        "$select": "id,subject,from,internetMessageId,bodyPreview,body",
        "$orderby": "receivedDateTime asc",
    }
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    items = payload.get("value", []) or []
    if not items:
        return 0, log

    for item in items:
        mid = (item.get("internetMessageId") or "").strip()
        gid = (item.get("id") or "").strip()
        if not mid:
            mid = f"graph:{gid}"
        if not mid:
            continue
        if SupportTicket.objects.filter(source_message_id=mid).exists():
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
            or mailbox
        )
        body = ""
        body_obj = item.get("body") or {}
        content = (body_obj.get("content") or "").strip()
        if content:
            if (body_obj.get("contentType") or "").lower() == "html":
                body = strip_tags(content).replace("\xa0", " ").strip()
            else:
                body = content
        if not body:
            body = (item.get("bodyPreview") or "").strip()
        if not body:
            body = "(empty body)"

        try:
            SupportTicket.objects.create(
                company=company,
                created_by=user,
                subject=subject,
                description=body,
                status=SupportTicket.Status.OPEN,
                source_message_id=mid,
                source_sender=sender[:254] if sender else "",
            )
            created += 1
        except IntegrityError:
            pass

        if mark_seen and gid:
            try:
                _graph_mark_read(access_token, mailbox, gid)
            except Exception:
                pass

    log.append(
        f"[{company.company_name}] Graph imported {created} new ticket(s) "
        f"from {len(items)} unread message(s)."
    )
    return created, log


def _detect_imap_host(email_addr: str) -> str:
    """
    Identify IMAP host from mailbox domain/MX.
    No SUPPORT_IMAP_HOST env dependence.
    """
    domain = (email_addr.split("@")[-1] or "").strip().lower()
    if not domain:
        return "imap.gmail.com"

    # Direct domain-based shortcuts.
    if domain in {"gmail.com", "googlemail.com"}:
        return "imap.gmail.com"
    if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
        return "outlook.office365.com"
    if domain.endswith(".yahoo.com") or domain.startswith("yahoo."):
        return "imap.mail.yahoo.com"
    if domain.endswith(".zoho.com") or domain == "zoho.com":
        return "imap.zoho.com"
    if "yandex" in domain:
        return "imap.yandex.com"

    # MX-based provider detection for custom domains.
    if dns is not None:
        try:
            answers = dns.resolver.resolve(domain, "MX")
            mx_hosts = sorted(
                (
                    str(r.exchange).rstrip(".").lower()
                    for r in answers
                    if getattr(r, "exchange", None)
                ),
                key=len,
            )
            joined = " ".join(mx_hosts)
            if "google.com" in joined or "googlemail.com" in joined:
                return "imap.gmail.com"
            if "protection.outlook.com" in joined or "outlook.com" in joined:
                return "outlook.office365.com"
            if "yahoodns.net" in joined or "yahoo.com" in joined:
                return "imap.mail.yahoo.com"
            if "zoho.com" in joined:
                return "imap.zoho.com"
            if "yandex.net" in joined or "yandex.ru" in joined:
                return "imap.yandex.com"
        except Exception:
            pass

    # Last fallback.
    return f"imap.{domain}"


def fetch_tickets_for_company(company: CompanyProfile) -> tuple[int, list[str]]:
    """Connect to the company's support IMAP account; create tickets for UNSEEN messages."""
    created = 0
    log: list[str] = []
    if not company.support_email or not company.support_email_app_password:
        return 0, log

    user = company.user
    host = _detect_imap_host(company.support_email)
    port = 993
    mailbox = settings.SUPPORT_IMAP_MAILBOX
    mark_seen = settings.SUPPORT_IMAP_MARK_SEEN

    # Microsoft 365 domains: prefer Graph OAuth (IMAP basic auth is often blocked).
    if _is_microsoft_host(host):
        try:
            log.append(f"[{company.company_name}] using Microsoft Graph for mailbox sync.")
            c, l = _fetch_via_graph(company, mark_seen=mark_seen)
            return c, log + l
        except Exception as e:
            log.append(f"[{company.company_name}] Graph sync error: {e}")

    imap: imaplib.IMAP4_SSL | None = None
    try:
        log.append(f"[{company.company_name}] using IMAP host: {host}")
        imap = imaplib.IMAP4_SSL(host, port, timeout=60)
        imap.login(company.support_email, company.support_email_app_password)
        typ, _ = imap.select(mailbox)
        if typ != "OK":
            log.append(f"[{company.company_name}] could not select mailbox {mailbox!r}.")
            return 0, log

        typ, data = imap.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return 0, log

        ids = data[0].split()
        if not ids:
            return 0, log

        for num in ids:
            raw = _fetch_rfc822_bytes(imap, num)
            if not raw:
                continue

            msg = message_from_bytes(raw, policy=email.policy.default)
            mid = (msg.get("Message-ID") or "").strip()
            if not mid:
                mid = stable_fallback_id(raw)

            if SupportTicket.objects.filter(source_message_id=mid).exists():
                if mark_seen:
                    imap.store(num, "+FLAGS", "\\Seen")
                continue

            subject = _decode_header(msg.get("Subject", "")) or "(no subject)"
            subject = subject.strip()[:255]
            from_decoded = _decode_header(msg.get("From", ""))
            sender = parse_rfc5322_addr(from_decoded) or from_decoded[:254]

            body = extract_plain_body(msg) or "(empty body)"

            try:
                SupportTicket.objects.create(
                    company=company,
                    created_by=user,
                    subject=subject,
                    description=body,
                    status=SupportTicket.Status.OPEN,
                    source_message_id=mid,
                    source_sender=sender[:254] if sender else "",
                )
                created += 1
            except IntegrityError:
                logger.info(
                    "Skip duplicate message-id %s for company %s", mid, company.pk
                )
                if mark_seen:
                    imap.store(num, "+FLAGS", "\\Seen")
                continue

            if mark_seen:
                styp, _ = imap.store(num, "+FLAGS", "\\Seen")
                if styp != "OK":
                    logger.warning(
                        "Could not mark message seen (company=%s num=%s)",
                        company.pk,
                        num,
                    )

        log.append(
            f"[{company.company_name}] imported {created} new ticket(s) "
            f"from {len(ids)} unread message(s)."
        )
    except imaplib.IMAP4.error as e:
        log.append(f"[{company.company_name}] IMAP error: {e}")
    except OSError as e:
        log.append(f"[{company.company_name}] connection error: {e}")
        logger.exception("IMAP connection error for company %s", company.pk)
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

    return created, log
