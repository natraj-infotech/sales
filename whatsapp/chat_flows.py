"""Multi-step WhatsApp chat flows: procure (lead), support, case lock."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from accounts.models import CompanyProfile
from business.catalog import CatalogProduct
from business.catalog_whatsapp import (
    categories_for_master,
    category_list_rows,
    master_list_rows,
    parse_category_reply_id,
    parse_master_reply_id,
    parse_product_reply_id,
    parse_subcategory_reply_id,
    product_list_rows,
    products_for_subcategory,
    subcategories_for_category,
    subcategory_list_rows,
)
from business.customer_quotation import (
    create_quotation_from_procure,
    quotation_whatsapp_messages,
)
from business.models import (
    CaseLockInvite,
    CaseLockProduct,
    CaseLockSubmission,
    Lead,
)

from .account_resolve import WhatsAppAccountLike, resolve_lead_company
from .chat_messaging import (
    BYE_THANK_YOU,
    STOP_ID,
    end_session,
    is_stop_intent,
    send_flow_buttons,
    send_flow_list,
    send_flow_prompt,
)
from .client import WhatsAppClientError, download_media, send_text_message
from .conversation_state import advance_step, reset_session, start_flow
from .models import WhatsAppConversation

logger = logging.getLogger(__name__)

WELCOME_BUTTONS = [
    {"id": "procure", "title": "Procure products"},
    {"id": "support", "title": "Support"},
    {"id": "caselock", "title": "Case lock"},
]

END_BUTTONS = [
    {"id": "something_else", "title": "Something else"},
    {"id": "bye_now", "title": "Bye for now"},
]

END_STEP = "await_end_choice"
TEXT_BYE_ALIASES = frozenset({"bye", "bye for now", "goodbye", "thanks", "thank you"})

WELCOME_LIST_ROWS = [
    {"id": "procure", "title": "Procure products", "description": "Request a quote"},
    {"id": "support", "title": "Support", "description": "Open a support ticket"},
    {"id": "caselock", "title": "Case lock", "description": "Submit case lock"},
]

ADD_SUBMIT_BUTTONS = [
    {"id": "add_more", "title": "Add more products"},
    {"id": "submit_req", "title": "Submit requirement"},
]

SUPPORT_ATTACH_PROMPT_BUTTONS = [
    {"id": "attach_yes", "title": "Yes, attach"},
    {"id": "attach_no", "title": "No"},
]

SUPPORT_ATTACH_DONE_BUTTONS = [
    {"id": "attach_done", "title": "Done"},
]

MAX_SUPPORT_ATTACHMENTS = 5


def send_main_welcome(*, account: WhatsAppAccountLike, to: str, body: str | None = None) -> None:
    from django.conf import settings

    text = (
        body
        or getattr(settings, "WHATSAPP_WELCOME_BODY", "")
        or "Welcome to Tracker365. How can we help you today?"
    ).strip()
    send_flow_list(
        account=account,
        to=to,
        body_text=text,
        button_label="Choose option",
        rows=WELCOME_LIST_ROWS,
    )


def _company(account: WhatsAppAccountLike) -> CompanyProfile | None:
    return resolve_lead_company(account)


def _owner_user(company: CompanyProfile | None):
    if company is None:
        return None
    return company.user


def _is_bye_intent(*, reply_id: str, text: str) -> bool:
    if reply_id in ("bye_now", STOP_ID):
        return True
    if is_stop_intent(reply_id=reply_id, text=text):
        return True
    normalized = (text or "").strip().lower()
    return normalized in TEXT_BYE_ALIASES


def handle_await_end_choice(
    *,
    account: WhatsAppAccountLike,
    session: WhatsAppConversation,
    to: str,
    text: str,
    reply_id: str,
) -> bool:
    """
    End-of-flow menu: Bye resets to idle with thanks; next user message starts welcome again.
    Something else only ends the flow (no welcome until a later message).
    Plain text after the flow restarts with the welcome menu on that message.
    """
    if _is_bye_intent(reply_id=reply_id, text=text):
        end_session(account=account, session=session, to=to)
        return True
    if reply_id == "something_else":
        reset_session(session)
        send_flow_prompt(
            account=account,
            to=to,
            body_text="We're here when you need us. Send a message anytime to open the menu again.",
        )
        return True
    if text and not reply_id:
        reset_session(session)
        send_main_welcome(account=account, to=to)
        return True
    if reply_id:
        send_flow_prompt(
            account=account,
            to=to,
            body_text="Please choose Bye for now or Something else, or send a new message.",
        )
        return True
    return False


def _send_master_menu(account: WhatsAppAccountLike, to: str, body: str) -> None:
    send_flow_list(
        account=account,
        to=to,
        body_text=body,
        button_label="Type",
        rows=master_list_rows(),
    )


def _maybe_send_subcategory_brochure(
    account: WhatsAppAccountLike,
    to: str,
    subcategory_id: int,
) -> None:
    from whatsapp.public_urls import public_media_url

    from business.catalog import CatalogSubcategory

    from .client import send_document_message

    sub = (
        CatalogSubcategory.objects.filter(pk=subcategory_id)
        .select_related("category")
        .first()
    )
    if sub is None or not sub.brochure_pdf:
        return
    url = public_media_url(sub.brochure_pdf.url)
    if not url:
        return
    try:
        send_document_message(
            account=account,
            to=to,
            link=url,
            filename=sub.brochure_pdf.name.split("/")[-1] or "brochure.pdf",
            caption=f"{sub.name} — brochure",
        )
    except WhatsAppClientError:
        logger.exception("Failed to send subcategory brochure subcategory_id=%s", subcategory_id)


def _send_end_menu(account: WhatsAppAccountLike, to: str) -> None:
    send_flow_buttons(
        account=account,
        to=to,
        body_text="Do you need something else?",
        buttons=END_BUTTONS,
    )


def _finalize_procure(
    *,
    account: WhatsAppAccountLike,
    lead: Lead,
    session: WhatsAppConversation,
    to: str,
    contact_name: str,
) -> None:
    draft = session.draft or {}
    lines = draft.get("lines") or []
    company = _company(account)
    _, quotation = create_quotation_from_procure(
        company=company,
        lead=lead,
        contact_name=contact_name or lead.contact_name,
        company_name=(draft.get("company_name") or "").strip(),
        phone=to,
        draft_lines=lines,
        whatsapp_wa_id=to,
    )
    advance_step(session, step="procure_done", draft_updates={})
    for body in quotation_whatsapp_messages(quotation):
        send_text_message(account=account, to=to, body=body)
    if quotation and quotation.pdf:
        from whatsapp.public_urls import public_media_url

        from .client import send_document_message

        doc_url = public_media_url(quotation.pdf.url)
        if doc_url:
            try:
                send_document_message(
                    account=account,
                    to=to,
                    link=doc_url,
                    filename=f"quotation_{quotation.pk}.pdf",
                    caption="Your quotation",
                )
            except WhatsAppClientError:
                logger.exception("Failed to send quotation PDF quote_id=%s", quotation.pk)
    _send_end_menu(account, to)
    advance_step(session, step=END_STEP)


def _handle_procure(
    *,
    account: WhatsAppAccountLike,
    lead: Lead,
    session: WhatsAppConversation,
    to: str,
    contact_name: str,
    text: str,
    reply_id: str,
) -> None:
    step = session.step
    draft = dict(session.draft or {})
    company = _company(account)

    if reply_id in ("add_more", "submit_req"):
        if reply_id == "add_more":
            advance_step(session, step="procure_master")
            _send_master_menu(account, to, "Choose Hardware, Software, or Others:")
            return
        _finalize_procure(
            account=account,
            lead=lead,
            session=session,
            to=to,
            contact_name=contact_name,
        )
        return

    master_key = parse_master_reply_id(reply_id)
    if master_key:
        categories = categories_for_master(company, master_key)
        if not categories:
            send_flow_prompt(
                account=account,
                to=to,
                body_text="No categories configured yet for this type. Please try another option.",
            )
            _send_master_menu(account, to, "Choose Hardware, Software, or Others:")
            return
        advance_step(
            session,
            step="procure_catalog_category",
            draft_updates={"master_category": master_key},
        )
        send_flow_list(
            account=account,
            to=to,
            body_text="Choose a category (e.g. Laptop, Desktop):",
            button_label="Categories",
            rows=category_list_rows(categories),
        )
        return

    category_id = parse_category_reply_id(reply_id)
    if category_id:
        from business.catalog import CatalogCategory

        cat = CatalogCategory.objects.filter(
            pk=category_id, company=company, is_active=True
        ).first()
        if cat is None:
            send_flow_prompt(account=account, to=to, body_text="Invalid category.")
            return
        subs = subcategories_for_category(category_id)
        if not subs:
            send_flow_prompt(
                account=account,
                to=to,
                body_text="No subcategories configured. Ask your supplier to add specs (e.g. i5).",
            )
            return
        advance_step(
            session,
            step="procure_subcategory",
            draft_updates={"catalog_category_id": category_id},
        )
        send_flow_list(
            account=account,
            to=to,
            body_text="Choose specification (e.g. processor):",
            button_label="Specs",
            rows=subcategory_list_rows(subs),
        )
        return

    subcategory_id = parse_subcategory_reply_id(reply_id)
    if subcategory_id:
        from business.catalog import CatalogSubcategory

        sub = (
            CatalogSubcategory.objects.filter(
                pk=subcategory_id,
                category__company=company,
                is_active=True,
            )
            .select_related("category")
            .first()
        )
        if sub is None:
            send_flow_prompt(account=account, to=to, body_text="Invalid subcategory.")
            return
        products = products_for_subcategory(subcategory_id)
        if not products:
            send_flow_prompt(
                account=account,
                to=to,
                body_text="No products listed yet for this specification.",
            )
            return
        _maybe_send_subcategory_brochure(account, to, subcategory_id)
        advance_step(
            session,
            step="procure_catalog_product",
            draft_updates={
                "catalog_subcategory_id": subcategory_id,
                "master_category": sub.category.master_category,
                "category_name": sub.category.name,
                "subcategory_name": sub.name,
            },
        )
        send_flow_list(
            account=account,
            to=to,
            body_text="Select a product:",
            button_label="Products",
            rows=product_list_rows(products),
        )
        return

    catalog_product_id = parse_product_reply_id(reply_id)
    if catalog_product_id:
        product = (
            CatalogProduct.objects.filter(
                pk=catalog_product_id,
                subcategory__category__company=company,
                is_active=True,
            )
            .select_related("subcategory__category")
            .first()
        )
        if product is None:
            send_flow_prompt(account=account, to=to, body_text="Product not found.")
            return
        unit_price = None
        if product.priced():
            unit_price = str(product.unit_price)
        advance_step(
            session,
            step="procure_quantity",
            draft_updates={
                "pending_catalog_product_id": product.pk,
                "pending_display_name": product.display_label(),
                "pending_unit_price": unit_price,
                "master_category": product.master_category,
                "category_name": product.category_name,
                "subcategory_name": product.subcategory_name,
            },
        )
        send_flow_prompt(account=account, to=to, body_text="Enter quantity (number):")
        return

    if step == "procure_company_name" and text:
        advance_step(session, step="procure_master", draft_updates={"company_name": text.strip()})
        _send_master_menu(account, to, "Choose Hardware, Software, or Others:")
        return

    if step == "procure_quantity" and text:
        try:
            qty = int(text.strip())
            if qty < 1:
                raise ValueError
        except ValueError:
            send_flow_prompt(
                account=account,
                to=to,
                body_text="Please enter a valid quantity (whole number ≥ 1).",
            )
            return
        lines = list(draft.get("lines") or [])
        lines.append(
            {
                "catalog_product_id": draft.get("pending_catalog_product_id"),
                "display_name": draft.get("pending_display_name"),
                "quantity": qty,
                "master_category": draft.get("master_category") or draft.get("pending_master"),
                "category_name": draft.get("category_name"),
                "subcategory_name": draft.get("subcategory_name"),
                "unit_price": draft.get("pending_unit_price"),
            }
        )
        advance_step(
            session,
            step="procure_add_submit",
            draft_updates={
                "lines": lines,
                "pending_catalog_product_id": None,
                "pending_display_name": None,
                "pending_unit_price": None,
            },
        )
        send_flow_buttons(
            account=account,
            to=to,
            body_text="Add another product or submit for quotation?",
            buttons=ADD_SUBMIT_BUTTONS,
        )
        return

    send_flow_prompt(
        account=account,
        to=to,
        body_text="Please use the buttons or reply as prompted.",
    )


def _download_support_attachments(
    *,
    account: WhatsAppAccountLike,
    draft: dict,
) -> list[tuple[str, bytes, str]]:
    files: list[tuple[str, bytes, str]] = []
    for item in draft.get("attachment_media") or []:
        if not isinstance(item, dict):
            continue
        mid = (item.get("media_id") or "").strip()
        if not mid:
            continue
        try:
            data, mime, name = download_media(account=account, media_id=mid)
        except WhatsAppClientError:
            logger.exception("WhatsApp support attachment download failed media_id=%s", mid)
            continue
        files.append((name, data, mime))
    return files


def _create_support_ticket(
    *,
    account: WhatsAppAccountLike,
    lead: Lead,
    session: WhatsAppConversation,
    to: str,
    message_id: str = "",
) -> None:
    from business.helpdesk_whatsapp import create_helpdesk_ticket_from_whatsapp

    company = _company(account)
    owner = _owner_user(company)
    if company is None or owner is None:
        send_flow_prompt(
            account=account,
            to=to,
            body_text="Support is not configured for this line. Please try again later.",
        )
        reset_session(session)
        return
    draft = session.draft or {}
    firm = (draft.get("firm_name") or "").strip()
    description = (draft.get("description") or "").strip()
    attachments = _download_support_attachments(account=account, draft=draft)
    ticket = create_helpdesk_ticket_from_whatsapp(
        company=company,
        firm_name=firm,
        description=description,
        phone_wa_id=to,
        attachments=attachments,
        message_id=message_id,
    )
    lead_updates = []
    if firm and lead.company_name != firm:
        lead.company_name = firm[:255]
        lead_updates.append("company_name")
    if to and not lead.phone:
        lead.phone = to[:64]
        lead_updates.append("phone")
    if lead_updates:
        lead_updates.append("updated_at")
        lead.save(update_fields=lead_updates)

    ref = f"TK-{ticket.pk:05d}"
    send_text_message(
        account=account,
        to=to,
        body=f"Support ticket {ref} created. We will follow up soon.",
    )
    advance_step(session, step=END_STEP)
    _send_end_menu(account, to)


def _submit_support_ticket(
    *,
    account: WhatsAppAccountLike,
    lead: Lead,
    session: WhatsAppConversation,
    to: str,
    message_id: str = "",
) -> None:
    advance_step(session, step="support_submit")
    _create_support_ticket(
        account=account,
        lead=lead,
        session=session,
        to=to,
        message_id=message_id,
    )


def _handle_support(
    *,
    account: WhatsAppAccountLike,
    lead: Lead,
    session: WhatsAppConversation,
    to: str,
    text: str,
    reply_id: str,
    media_id: str = "",
    media_mime: str = "",
    message_id: str = "",
) -> None:
    if reply_id == "attach_yes":
        advance_step(
            session,
            step="support_collect_images",
            draft_updates={"attachment_media": []},
        )
        send_flow_buttons(
            account=account,
            to=to,
            body_text="Send your image(s), then tap Done when finished.",
            buttons=SUPPORT_ATTACH_DONE_BUTTONS,
        )
        return

    if reply_id == "attach_no":
        _submit_support_ticket(
            account=account,
            lead=lead,
            session=session,
            to=to,
            message_id=message_id,
        )
        return

    if reply_id == "attach_done" and session.step == "support_collect_images":
        _submit_support_ticket(
            account=account,
            lead=lead,
            session=session,
            to=to,
            message_id=message_id,
        )
        return

    if session.step == "support_collect_images" and media_id:
        draft = dict(session.draft or {})
        items = list(draft.get("attachment_media") or [])
        if len(items) >= MAX_SUPPORT_ATTACHMENTS:
            send_flow_prompt(
                account=account,
                to=to,
                body_text=(
                    f"You can attach up to {MAX_SUPPORT_ATTACHMENTS} images. "
                    "Tap Done to submit your ticket."
                ),
            )
            return
        items.append({"media_id": media_id, "mime_type": media_mime or "image/jpeg"})
        advance_step(session, step="support_collect_images", draft_updates={"attachment_media": items})
        send_flow_buttons(
            account=account,
            to=to,
            body_text=f"Image received ({len(items)}/{MAX_SUPPORT_ATTACHMENTS}). Send more or tap Done.",
            buttons=SUPPORT_ATTACH_DONE_BUTTONS,
        )
        return

    if session.step == "support_firm_name" and text:
        advance_step(
            session,
            step="support_description",
            draft_updates={"firm_name": text.strip()[:255]},
        )
        send_flow_prompt(account=account, to=to, body_text="Please describe the issue in detail:")
        return

    if session.step == "support_description" and text:
        advance_step(
            session,
            step="support_attach_prompt",
            draft_updates={"description": text.strip()},
        )
        send_flow_buttons(
            account=account,
            to=to,
            body_text="Do you want to attach any images?",
            buttons=SUPPORT_ATTACH_PROMPT_BUTTONS,
        )
        return

    send_flow_prompt(
        account=account,
        to=to,
        body_text="Please use the buttons or reply as prompted.",
    )


def _create_caselock_submission(
    *,
    account: WhatsAppAccountLike,
    session: WhatsAppConversation,
    to: str,
) -> None:
    company = _company(account)
    if company is None:
        send_flow_prompt(
            account=account,
            to=to,
            body_text="Case lock is not available on this line.",
        )
        reset_session(session)
        return
    draft = session.draft or {}
    product = CaseLockProduct.objects.filter(
        pk=draft.get("caselock_product_id"), company=company
    ).first()
    if product is None:
        send_flow_prompt(
            account=account,
            to=to,
            body_text="Product not found. Please start Case lock again.",
        )
        reset_session(session)
        return
    msg_key = f"wa:{to}:{uuid.uuid4()}"
    invite = CaseLockInvite.objects.create(
        company=company,
        source_message_id=msg_key[:998],
        sender_email=f"{to}@whatsapp.local",
        mail_subject="WhatsApp case lock",
    )
    CaseLockSubmission.objects.create(
        invite=invite,
        client_name=(draft.get("client_name") or "")[:255],
        client_gst=(draft.get("client_gst") or "")[:32],
        client_phone=to[:64],
        product=product,
        product_name=product.name,
        product_quantity=int(draft.get("quantity") or 1),
    )
    send_text_message(
        account=account,
        to=to,
        body="Your case lock request has been submitted.",
    )
    advance_step(session, step=END_STEP)
    _send_end_menu(account, to)


def _caselock_product_list(account: WhatsAppAccountLike, to: str, company: CompanyProfile | None) -> None:
    products = list(
        CaseLockProduct.objects.filter(company=company).order_by("name")[:10]
    )
    if not products:
        send_flow_prompt(
            account=account,
            to=to,
            body_text="No case lock products are configured. Please contact support.",
        )
        return
    rows = [{"id": f"clprod_{p.pk}", "title": p.name[:24], "description": ""} for p in products]
    send_flow_list(
        account=account,
        to=to,
        body_text="Select the product for case lock:",
        button_label="Products",
        rows=rows,
    )


def _handle_caselock(
    *,
    account: WhatsAppAccountLike,
    lead: Lead,
    session: WhatsAppConversation,
    to: str,
    text: str,
    reply_id: str,
) -> None:
    company = _company(account)

    if reply_id.startswith("clprod_"):
        try:
            pk = int(reply_id.split("_", 1)[1])
        except (IndexError, ValueError):
            send_flow_prompt(account=account, to=to, body_text="Invalid product.")
            return
        advance_step(
            session,
            step="caselock_quantity",
            draft_updates={"caselock_product_id": pk},
        )
        send_flow_prompt(account=account, to=to, body_text="Enter quantity (number):")
        return

    if session.step == "caselock_client_name" and text:
        advance_step(session, step="caselock_client_gst", draft_updates={"client_name": text.strip()})
        send_flow_prompt(account=account, to=to, body_text="Enter GST number:")
        return

    if session.step == "caselock_client_gst" and text:
        advance_step(session, step="caselock_product", draft_updates={"client_gst": text.strip()})
        _caselock_product_list(account, to, company)
        return

    if session.step == "caselock_quantity" and text:
        try:
            qty = int(text.strip())
            if qty < 1:
                raise ValueError
        except ValueError:
            send_flow_prompt(account=account, to=to, body_text="Please enter a valid quantity (≥ 1).")
            return
        advance_step(session, step="caselock_submit", draft_updates={"quantity": qty})
        _create_caselock_submission(account=account, session=session, to=to)
        return

    send_flow_prompt(
        account=account,
        to=to,
        body_text="Please use the buttons or reply as prompted.",
    )


def start_flow_from_welcome(
    *,
    account: WhatsAppAccountLike,
    lead: Lead,
    session: WhatsAppConversation,
    to: str,
    reply_id: str,
) -> None:
    if is_stop_intent(reply_id=reply_id, text=""):
        end_session(account=account, session=session, to=to)
        return
    flow_id = reply_id
    if flow_id in ("lead", "procure"):
        start_flow(session, flow="procure", step="procure_company_name", draft={"lines": []})
        send_flow_prompt(account=account, to=to, body_text="Please enter your company name:")
        return
    if flow_id == "support":
        start_flow(session, flow="support", step="support_firm_name", draft={})
        send_flow_prompt(
            account=account,
            to=to,
            body_text="Please enter your customer or firm name:",
        )
        return
    if flow_id == "caselock":
        start_flow(session, flow="caselock", step="caselock_client_name", draft={})
        send_flow_prompt(
            account=account,
            to=to,
            body_text="Case lock — enter your company name (customer name):",
        )
        return


def route_active_flow(
    *,
    account: WhatsAppAccountLike,
    lead: Lead,
    session: WhatsAppConversation,
    to: str,
    contact_name: str,
    text: str,
    reply_id: str,
    media_id: str = "",
    media_mime: str = "",
    message_id: str = "",
) -> None:
    if is_stop_intent(reply_id=reply_id, text=text):
        end_session(account=account, session=session, to=to)
        return

    if session.step == END_STEP:
        if handle_await_end_choice(
            account=account,
            session=session,
            to=to,
            text=text,
            reply_id=reply_id,
        ):
            return

    flow = session.flow
    if flow == "procure":
        _handle_procure(
            account=account,
            lead=lead,
            session=session,
            to=to,
            contact_name=contact_name,
            text=text,
            reply_id=reply_id,
        )
    elif flow == "support":
        _handle_support(
            account=account,
            lead=lead,
            session=session,
            to=to,
            text=text,
            reply_id=reply_id,
            media_id=media_id,
            media_mime=media_mime,
            message_id=message_id,
        )
    elif flow == "caselock":
        _handle_caselock(
            account=account,
            lead=lead,
            session=session,
            to=to,
            text=text,
            reply_id=reply_id,
        )
    else:
        reset_session(session)
        send_main_welcome(account=account, to=to)
