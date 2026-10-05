"""Normalize inbound support-email bodies (strip Exchange external banners, etc.)."""

from __future__ import annotations

import html
import re

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
    text = _EXTERNAL_CAUTION.sub(" ", text)
    text = _CAUTION_DO_NOT_CLICK.sub(" ", text)
    text = _EXTERNAL_ORIGIN_ONLY.sub(" ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if not text:
        return empty_fallback if empty_fallback is not None else ""
    return text
