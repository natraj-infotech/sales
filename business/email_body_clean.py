"""Normalize inbound support-email bodies (strip banners, signatures, quoted history)."""

from __future__ import annotations

import html
import re
from django.utils.html import strip_tags

# Shown when the only content was an auto-injected mail-security banner.
ASSIGN_NEEDED_MESSAGE = "This ticket needs to be assigned."

# Banner end: "...know the content is safe. Report any suspicious email ... IT Team."
_BANNER_END = (
    r"(?:know the content is safe\.(?:\s*Report any suspicious email[^.]*\.)?"
    r"|Report any suspicious email[^.]*\."
    r"|to IT Team\.)"
)

# M365 / org gateways inject something like:
# "CAUTION: This email originated from outside of Acme. Do not click links..."
# HTML-to-text can glue it to the previous word ("helloCAUTION:"), so no
# leading whitespace is required.
_EXTERNAL_CAUTION = re.compile(
    r"(?is)"
    r"(?:\*+\s*)?"
    r"(?:CAUTION|WARNING)\s*:?\s*"
    r"This email originated from outside\b"
    r".*?"
    r"(?:" + _BANNER_END + r"|\Z)"
)

# "CAUTION: <anything> Do not click links ... IT Team." without the origin line.
_CAUTION_DO_NOT_CLICK = re.compile(
    r"(?is)"
    r"(?:\*+\s*)?"
    r"(?:CAUTION|WARNING)\s*:\s*"
    r"[^\n]{0,300}?Do not click links"
    r".*?" + _BANNER_END
)

_EXTERNAL_ORIGIN_ONLY = re.compile(
    r"(?i)"
    r"This email originated from outside of "
    r"[^\n]*?"
    r"(?:" + _BANNER_END + r"|(?=\n)|\Z)"
)

# Letterhead / signature containers and quoted-reply blocks in HTML mail.
_HTML_CHROME = re.compile(
    r"(?is)<(div|table|section|td|blockquote)\b[^>]*"
    r"(?:id|class)\s*=\s*[\"'][^\"']*"
    r"(?:signature|gmail_signature|gmail_quote|divRplyFwdMsg|appendonsend"
    r"|x_signature|x_divRplyFwdMsg|moz-cite-prefix"
    r"|mail-editor-reference-message-container)"
    r"[^\"']*[\"'][^>]*>.*"
)

# Outlook names letterhead and signature images image001.png, logo.png, and so on.
_CHROME_FILE = re.compile(
    r"(?i)^(?:image\d+|logo|header|footer|banner|letterhead|signature"
    r"|company[-_ ]?logo)"
    r"(?:[-_.][\w.-]*)?\.(?:png|jpe?g|gif|bmp|webp|emf|wmf)$"
)

# Sign-off through the end of the message. Matches mid-line too, because
# HTML-to-text used to glue "moduleThank you.Best Regards".
_SIGNOFF = re.compile(
    r"(?is)\b("
    r"best\s+regards"
    r"|kind\s+regards"
    r"|warm\s+regards"
    r"|thanks\s*(?:&|and)\s*regards"
    r"|sent\s+from\s+my\s+"
    r"|get\s+outlook\s+for"
    r"|this\s+(?:e-?mail|message)\s+"
    r"(?:and any|is confidential|including any attachments)"
    r"|_{8,}"
    r"|-{5,}\s*original\s+message"
    r")\b.*"
)
_SIGNOFF_LINE = re.compile(r"(?is)(?:^|\n)[ \t]*regards[ \t]*,.*")

_QUOTED_HISTORY = re.compile(
    r"(?is)(?:"
    r"-{2,}\s*original\s+message\s*-{2,}.*"
    r"|\n[ \t]*on .{0,240}?wrote:\s*.*"
    r"|\n[ \t]*from:\s.+\n[ \t]*sent:\s.*"
    r")"
)

_TRAILING_THANKS = re.compile(
    r"(?is)(?:\s|^)(?:thank\s*you|thanks)[.!]?\s*$"
)
_GLUED_THANKS = re.compile(r"(?i)thank\s*you[.!]?\s*$")

# Leading letterhead lines: company legal name, phone, or web address.
# The whole line must be that chrome, so a real sentence is left alone.
_CONTACT_LINE = re.compile(
    r"(?ix)^(?:"
    r"(?:mob(?:ile)?|ph(?:one)?|tel|email|e-mail|web|website)\s*[:.\-].+"
    r"|\+?\d[\d\s().\-]{7,}\d"
    r"|https?://\S+"
    r"|www\.\S+"
    r"|[A-Za-z0-9][\w .,&'’-]{1,70}"
    r"\b(?:pvt\.?\s*ltd\.?|private\s+limited|llc|llp)\b\.?"
    r")$"
)


def is_mail_chrome_filename(name: str) -> bool:
    """True for Outlook letterhead, signature, and auto-named inline images."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return bool(_CHROME_FILE.match(base))


def _clip_letterhead(text: str) -> str:
    """Drop leading company-name, phone, and website lines."""
    lines = text.splitlines()
    index = 0
    removed = False
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        if _CONTACT_LINE.match(stripped):
            removed = True
            index += 1
            continue
        break
    if not removed:
        return text
    rest = "\n".join(lines[index:]).strip()
    return rest or text


def html_to_plain(raw_html: str) -> str:
    """Turn a mail HTML body into text, keeping paragraph breaks."""
    text = raw_html or ""
    text = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", text)
    text = _HTML_CHROME.sub(" ", text)
    text = re.sub(r"(?i)</(span|a|strong|b|em|font)>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(
        r"(?i)</(p|div|tr|li|h[1-6]|table|blockquote|section|td)>",
        "\n",
        text,
    )
    return strip_tags(text)


def _clip_signoff(text: str) -> str:
    text = _QUOTED_HISTORY.sub("", text)
    text = _SIGNOFF.sub("", text)
    text = _SIGNOFF_LINE.sub("", text).strip()
    spaced = _TRAILING_THANKS.sub("", text).strip()
    if spaced and spaced != text:
        return spaced
    glued = _GLUED_THANKS.sub("", text).strip()
    if glued and glued != text:
        return glued
    return text


def inline_cids_in_main_body(raw_html: str) -> set[str]:
    """
    Content-IDs of images that sit in the message itself.

    Images in the letterhead (before the text) and in the signature
    (after the sign-off) are left out.
    """
    if not raw_html or "<" not in raw_html:
        return set()
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return set()

    clipped = _HTML_CHROME.sub(" ", raw_html)
    clipped = _SIGNOFF.sub("", clipped)
    clipped = _SIGNOFF_LINE.sub("", clipped)
    soup = BeautifulSoup(clipped, "html.parser")
    kept: set[str] = set()
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        match = re.search(r"cid:([^\"'\s>]+)", src, re.I)
        if not match:
            continue
        before_len = 0
        for node in img.previous_elements:
            if isinstance(node, str):
                before_len += len(node.strip())
            if before_len > 80:
                break
        if before_len < 12:
            continue
        kept.add(match.group(1).strip().strip("<>").lower())
    return kept


def plain_main_body(
    content: str,
    content_type: str = "text",
    *,
    empty_fallback: str | None = ASSIGN_NEEDED_MESSAGE,
) -> str:
    """Main message text from a Graph body, without header or footer."""
    raw = content or ""
    if (content_type or "").lower() == "html" or "<html" in raw[:500].lower() or "<div" in raw[:500].lower():
        raw = html_to_plain(raw)
    return clean_inbound_email_body(raw, empty_fallback=empty_fallback)


def clean_inbound_email_body(
    raw: str,
    *,
    empty_fallback: str | None = ASSIGN_NEEDED_MESSAGE,
) -> str:
    """
    Decode entities, drop external-mail CAUTION banners, normalize whitespace.

    If nothing useful remains, return ``empty_fallback`` (default: assign-needed note).
    Pass ``empty_fallback=""`` to leave the description blank instead.
    """
    text = html.unescape(raw or "")
    text = (
        text.replace("\xa0", " ")
        .replace("\u00a0", " ")
        .replace("\u200b", "")
        .replace("&nbsp;", " ")
    )
    text = _clip_signoff(text)
    text = _clip_letterhead(text)
    text = _EXTERNAL_CAUTION.sub(" ", text)
    text = _CAUTION_DO_NOT_CLICK.sub(" ", text)
    text = _EXTERNAL_ORIGIN_ONLY.sub(" ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if not text:
        return empty_fallback if empty_fallback is not None else ""
    return text
