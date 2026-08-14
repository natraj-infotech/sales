"""Normalize inbound support-email bodies (strip Exchange external banners, etc.)."""

from __future__ import annotations

import html
import re

# Shown when the only content was an auto-injected mail-security banner.
ASSIGN_NEEDED_MESSAGE = "This ticket needs to be assigned."

# M365 / org gateways prepend something like:
# "CAUTION: This email originated from outside of Acme. Do not click links..."
_EXTERNAL_CAUTION = re.compile(
    r"(?is)"
    r"(?:^|\s)"
    r"(?:\*+\s*)?"
    r"(?:CAUTION|WARNING)\s*:?\s*"
    r"This email originated from outside\b"
    r".*?"
    r"(?:Report any suspicious email[^.]*\.|to IT Team\.|$)"
)

_EXTERNAL_ORIGIN_ONLY = re.compile(
    r"(?is)"
    r"(?:^|\s)"
    r"This email originated from outside of .+?"
    r"(?:Do not click links[^.]*\.)?"
    r"(?:\s*Report any suspicious email[^.]*\.)?"
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
    text = _EXTERNAL_ORIGIN_ONLY.sub(" ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if not text:
        return empty_fallback if empty_fallback is not None else ""
    return text
