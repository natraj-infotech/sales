"""Multi-step WhatsApp chat flows: procure (lead), support, case lock."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from django.db.models import Q

from accounts.models import CompanyProfile
from business.lead_ingest import create_lead_from_whatsapp
from business.models import (
    CaseLockInvite,
    CaseLockProduct,
    CaseLockSubmission,
    Lead,
    Product,
    SupportTicket,
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
from .client import WhatsAppClientError, send_text_message
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

CATEGORY_LIST_ROWS = [
    {"id": "cat_hardware", "title": "Hardware", "description": ""},
    {"id": "cat_software", "title": "Software", "description": ""},
    {"id": "cat_others", "title": "Others", "description": ""},
]

DEPT_LIST_ROWS = [
    {"id": "dept_general", "title": "General", "description": ""},
    {"id": "dept_billing", "title": "Billing", "description": ""},
    {"id": "dept_technical", "title": "Technical", "description": ""},
]

PRIORITY_LIST_ROWS = [
    {"id": "pri_low", "title": "Low", "description": ""},
    {"id": "pri_medium", "title": "Medium", "description": ""},
    {"id": "pri_high", "title": "High", "description": ""},
]

ADD_SUBMIT_BUTTONS = [
    {"id": "add_more", "title": "Add more products"},
    {"id": "submit_req", "title": "Submit requirement"},
]

CATEGORY_BUTTONS = [
    {"id": "cat_hardware", "title": "Hardware"},
    {"id": "cat_software", "title": "Software"},
    {"id": "cat_others", "title": "Others"},
]

DEPT_BUTTONS = [
    {"id": "dept_general", "title": "General"},
    {"id": "dept_billing", "title": "Billing"},
    {"id": "dept_technical", "title": "Technical"},
]

PRIORITY_BUTTONS = [
    {"id": "pri_low", "title": "Low"},
    {"id": "pri_medium", "title": "Medium"},
    {"id": "pri_high", "title": "High"},
]


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


def _products_queryset(company: CompanyProfile | None, category_key: str):
    if company is None:
        return Product.objects.none()
    qs = Product.objects.filter(company=company).order_by("name")
    if category_key == "hardware":
        return qs.filter(
            Q(category__icontains="hardware") | Q(asset_type__icontains="hardware")
        )
    if category_key == "software":
        return qs.filter(
            Q(category__icontains="software") | Q(asset_type__icontains="software")
        )
    return qs.exclude(
        Q(category__icontains="hardware")
        | Q(category__icontains="software")
        | Q(asset_type__icontains="hardware")
        | Q(asset_type__icontains="software")
    )


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


def _send_category_menu(account: WhatsAppAccountLike, to: str, body: str) -> None:
    send_flow_list(
        account=account,
        to=to,
        body_text=body,
        button_label="Categories",
        rows=CATEGORY_LIST_ROWS,
    )


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
    requirement_parts = []
    product_ids: list[int] = []
    for line in lines:
        requirement_parts.append(
            f"- {line.get('product_name')} x {line.get('quantity')} ({line.get('category')})"
        )
        if line.get("product_id"):
            product_ids.append(int(line["product_id"]))
    requirement = "\n".join(requirement_parts)
    create_lead_from_whatsapp(
        company=company,
        contact_name=contact_name or lead.contact_name,
        company_name=(draft.get("company_name") or "").strip(),
        phone=to,
        requirement=requirement,
        product_ids=product_ids,
        whatsapp_wa_id=to,
        raw_payload={"whatsapp_flow": "procure", "lines": lines},
    )
    advance_step(session, step="procure_done", draft_updates={})
    send_text_message(
        account=account,
        to=to,
        body="Your requirement has been submitted. Our team will contact you soon.",
    )
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
            advance_step(session, step="procure_category")
            _send_category_menu(account, to, "Choose a product category:")
            return
        _finalize_procure(
            account=account,
            lead=lead,
            session=session,
            to=to,
            contact_name=contact_name,
        )
        return

    if reply_id in ("cat_hardware", "cat_software", "cat_others"):
        key = reply_id.replace("cat_", "")
        products = list(_products_queryset(company, key)[:10])
        if not products:
            send_flow_prompt(
                account=account,
                to=to,
                body_text="No products found in that category. Please pick another category.",
            )
            _send_category_menu(account, to, "Choose a product category:")
            return
        advance_step(
            session,
            step="procure_product",
            draft_updates={
                "category": key,
                "product_choices": [p.pk for p in products],
            },
        )
        rows = [
            {
                "id": f"prod_{p.pk}",
                "title": p.name[:24],
                "description": (p.category or p.asset_type or "")[:72],
            }
            for p in products
        ]
        send_flow_list(
            account=account,
            to=to,
            body_text="Select a product from the list:",
            button_label="View products",
            rows=rows,
        )
        return

    if reply_id.startswith("prod_"):
        try:
            product_id = int(reply_id.split("_", 1)[1])
        except (IndexError, ValueError):
            send_flow_prompt(account=account, to=to, body_text="Invalid product. Please try again.")
            return
        product = Product.objects.filter(pk=product_id, company=company).first()
        if product is None:
            send_flow_prompt(account=account, to=to, body_text="Product not found. Please choose again.")
            return
        advance_step(
            session,
            step="procure_quantity",
            draft_updates={
                "pending_product_id": product.pk,
                "pending_product_name": product.name,
            },
        )
        send_flow_prompt(account=account, to=to, body_text="Enter quantity (number):")
        return

    if step == "procure_company_name" and text:
        advance_step(session, step="procure_category", draft_updates={"company_name": text.strip()})
        _send_category_menu(account, to, "Choose a product category:")
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
                "product_id": draft.get("pending_product_id"),
                "product_name": draft.get("pending_product_name"),
                "quantity": qty,
                "category": draft.get("category"),
            }
        )
        advance_step(
            session,
            step="procure_add_submit",
            draft_updates={
                "lines": lines,
                "pending_product_id": None,
                "pending_product_name": None,
            },
        )
        send_flow_buttons(
            account=account,
            to=to,
            body_text="Add another product or submit your requirement?",
            buttons=ADD_SUBMIT_BUTTONS,
        )
        return

    send_flow_prompt(
        account=account,
        to=to,
        body_text="Please use the buttons or reply as prompted.",
    )


def _create_support_ticket(
    *,
    account: WhatsAppAccountLike,
    lead: Lead,
    session: WhatsAppConversation,
    to: str,
) -> None:
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
    SupportTicket.objects.create(
        company=company,
        created_by=owner,
        subject=(draft.get("subject") or "WhatsApp support")[:255],
        description=(draft.get("description") or "")
        + f"\n\n— Submitted via WhatsApp ({to})",
        department=draft.get("department") or SupportTicket.Department.GENERAL,
        priority=draft.get("priority") or SupportTicket.Priority.MEDIUM,
        status=SupportTicket.Status.OPEN,
        source_sender=to[:254],
    )
    send_text_message(
        account=account,
        to=to,
        body=f"Support ticket created ({draft.get('subject', 'WhatsApp')}). We will follow up soon.",
    )
    advance_step(session, step=END_STEP)
    _send_end_menu(account, to)


def _handle_support(
    *,
    account: WhatsAppAccountLike,
    lead: Lead,
    session: WhatsAppConversation,
    to: str,
    text: str,
    reply_id: str,
) -> None:
    if reply_id.startswith("dept_"):
        dept_map = {
            "dept_general": SupportTicket.Department.GENERAL,
            "dept_billing": SupportTicket.Department.BILLING,
            "dept_technical": SupportTicket.Department.TECHNICAL,
        }
        advance_step(
            session,
            step="support_priority",
            draft_updates={"department": dept_map.get(reply_id, SupportTicket.Department.GENERAL)},
        )
        send_flow_list(
            account=account,
            to=to,
            body_text="Choose priority:",
            button_label="Priority",
            rows=PRIORITY_LIST_ROWS,
        )
        return

    if reply_id.startswith("pri_"):
        pri_map = {
            "pri_low": SupportTicket.Priority.LOW,
            "pri_medium": SupportTicket.Priority.MEDIUM,
            "pri_high": SupportTicket.Priority.HIGH,
        }
        advance_step(
            session,
            step="support_submit",
            draft_updates={"priority": pri_map.get(reply_id, SupportTicket.Priority.MEDIUM)},
        )
        _create_support_ticket(account=account, lead=lead, session=session, to=to)
        return

    if session.step == "support_subject" and text:
        advance_step(session, step="support_description", draft_updates={"subject": text.strip()[:255]})
        send_flow_prompt(account=account, to=to, body_text="Please describe the issue in detail:")
        return

    if session.step == "support_description" and text:
        advance_step(session, step="support_department", draft_updates={"description": text.strip()})
        send_flow_list(
            account=account,
            to=to,
            body_text="Choose department:",
            button_label="Department",
            rows=DEPT_LIST_ROWS,
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
        start_flow(session, flow="support", step="support_subject", draft={})
        send_flow_prompt(
            account=account,
            to=to,
            body_text="What is the subject of your support request?",
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
