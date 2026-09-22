"""Check Meta WhatsApp webhook / WABA subscription and optional repair."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from whatsapp.graph_api import (
    GraphAPIError,
    api_version,
    get_phone_number,
    get_subscribed_apps,
    register_whatsapp_webhook,
    subscribe_waba,
)
from whatsapp.models import WhatsAppAccount


class Command(BaseCommand):
    help = (
        "Diagnose why live WhatsApp messages may not POST to /whatsapp/webhook/. "
        "Use --subscribe to POST WABA subscribed_apps (and app subscriptions if configured)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--waba-id",
            default="",
            help="WhatsApp Business Account ID (or WHATSAPP_WABA_ID in .env).",
        )
        parser.add_argument(
            "--phone-number-id",
            default="",
            help="Phone number ID (or WHATSAPP_DEFAULT_PHONE_NUMBER_ID / first WhatsAppAccount).",
        )
        parser.add_argument(
            "--subscribe",
            action="store_true",
            help="Subscribe WABA to this app (+ register webhook if WHATSAPP_META_APP_ID set).",
        )

    def handle(self, *args, **options):
        waba_id = (options["waba_id"] or getattr(settings, "WHATSAPP_WABA_ID", "") or "").strip()
        phone_id = (
            options["phone_number_id"]
            or getattr(settings, "WHATSAPP_DEFAULT_PHONE_NUMBER_ID", "")
            or ""
        ).strip()

        if not phone_id:
            acc = WhatsAppAccount.objects.filter(is_active=True).first()
            if acc:
                phone_id = acc.phone_number_id.strip()

        self.stdout.write(self.style.NOTICE("=== WhatsApp webhook diagnose ==="))
        self.stdout.write(f"Graph API version: {api_version()}")
        self.stdout.write(f"ALLOWED_HOSTS: {settings.ALLOWED_HOSTS}")
        public = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip()
        if public:
            self.stdout.write(f"Expected webhook URL: {public.rstrip('/')}/whatsapp/webhook/")
        else:
            self.stdout.write(
                "PUBLIC_BASE_URL not set — ensure Meta callback matches your live HTTPS host."
            )

        if not (getattr(settings, "WHATSAPP_ACCESS_TOKEN", "") or "").strip():
            raise CommandError("WHATSAPP_ACCESS_TOKEN is missing in .env")

        accounts = list(WhatsAppAccount.objects.filter(is_active=True))
        self.stdout.write(f"Active WhatsAppAccount rows in DB: {len(accounts)}")
        for acc in accounts:
            self.stdout.write(
                f"  - company_id={acc.company_id} phone_number_id={acc.phone_number_id} "
                f"display={acc.display_phone or '—'}"
            )

        if phone_id:
            self.stdout.write(self.style.NOTICE(f"\n--- Phone number {phone_id} ---"))
            try:
                info = get_phone_number(phone_id)
            except GraphAPIError as exc:
                self.stderr.write(self.style.ERROR(f"Phone lookup failed: {exc}"))
                if exc.payload:
                    self.stderr.write(str(exc.payload))
            else:
                for key in (
                    "display_phone_number",
                    "verified_name",
                    "platform_type",
                    "quality_rating",
                    "code_verification_status",
                    "webhook_configuration",
                ):
                    if key in info:
                        self.stdout.write(f"  {key}: {info[key]}")
                db_match = any(a.phone_number_id == phone_id for a in accounts)
                if not db_match:
                    self.stderr.write(
                        self.style.WARNING(
                            "  No WhatsAppAccount with this phone_number_id — "
                            "Leads/welcome will not run (logs may still be created)."
                        )
                    )
        else:
            self.stdout.write(
                self.style.WARNING("No phone_number_id — pass --phone-number-id or add WhatsAppAccount.")
            )

        if waba_id:
            self.stdout.write(self.style.NOTICE(f"\n--- WABA {waba_id} subscribed_apps ---"))
            try:
                apps = get_subscribed_apps(waba_id)
            except GraphAPIError as exc:
                self.stderr.write(self.style.ERROR(f"subscribed_apps failed: {exc}"))
            else:
                data = apps.get("data") or []
                if data:
                    for row in data:
                        self.stdout.write(f"  subscribed app: {row}")
                else:
                    self.stderr.write(
                        self.style.ERROR(
                            "  No apps subscribed to this WABA — live message webhooks will NOT fire. "
                            "Run with --subscribe or click Subscribe in Meta → WhatsApp → Configuration."
                        )
                    )

            if options["subscribe"]:
                self.stdout.write("Subscribing WABA to this app…")
                try:
                    result = subscribe_waba(waba_id)
                    self.stdout.write(self.style.SUCCESS(f"  subscribed_apps: {result}"))
                except GraphAPIError as exc:
                    raise CommandError(f"subscribe_waba failed: {exc}") from exc
        else:
            self.stdout.write(
                self.style.WARNING(
                    "\nWHATSAPP_WABA_ID not set — pass --waba-id to check subscribed_apps."
                )
            )

        app_id = (getattr(settings, "WHATSAPP_META_APP_ID", "") or "").strip()
        verify = (getattr(settings, "WHATSAPP_VERIFY_TOKEN", "") or "").strip()
        callback = ""
        if public:
            callback = f"{public.rstrip('/')}/whatsapp/webhook/"

        if options["subscribe"] and app_id and callback and verify:
            self.stdout.write("Registering app webhook subscription (messages)…")
            try:
                result = register_whatsapp_webhook(
                    app_id=app_id,
                    callback_url=callback,
                    verify_token=verify,
                )
                self.stdout.write(self.style.SUCCESS(f"  subscriptions: {result}"))
            except GraphAPIError as exc:
                self.stderr.write(
                    self.style.WARNING(
                        f"  App subscription API failed (UI subscribe may still be OK): {exc}"
                    )
                )
        elif options["subscribe"] and not app_id:
            self.stdout.write(
                "Set WHATSAPP_META_APP_ID to register webhook via API (optional if done in Meta UI)."
            )

        self.stdout.write(
            self.style.NOTICE(
                "\nIf Meta Test works but real messages do not: send a real message, then check "
                "server logs for 'WhatsApp webhook POST' and Admin → WhatsApp message logs."
            )
        )
