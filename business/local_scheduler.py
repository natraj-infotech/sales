"""Background jobs while using Django runserver.

Production uses Celery beat. Locally, only `runserver` is usually running,
so this daemon thread runs every minute for as long as the server is up:
expiry reminders, team notifications, and support-mailbox ticket import.
Support mail is read even when nobody is logged in and the ticket page is closed.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)

TICK_SECONDS = 60
START_DELAY_SECONDS = 8

_started = False
_start_lock = threading.Lock()
_tick_lock = threading.Lock()


def should_start() -> bool:
    if not any(arg == "runserver" or str(arg).endswith("runserver") for arg in sys.argv):
        return False
    # Autoreload parent must not start a second ticker. `--noreload` never
    # sets RUN_MAIN, so start in that process instead.
    run_main = os.environ.get("RUN_MAIN")
    if run_main is None:
        return True
    return run_main == "true"


def start_if_runserver() -> None:
    global _started
    if not should_start():
        return
    with _start_lock:
        if _started:
            return
        _started = True
    thread = threading.Thread(
        target=_loop,
        name="local-scheduler",
        daemon=True,
    )
    thread.start()
    logger.warning(
        "Local scheduler started (every %ss). Expiry mail, team notifications, "
        "and support inbox fetch run while runserver is up, even if nobody is logged in.",
        TICK_SECONDS,
    )


def _loop() -> None:
    time.sleep(START_DELAY_SECONDS)
    while True:
        _tick()
        time.sleep(TICK_SECONDS)


def _tick() -> None:
    if not _tick_lock.acquire(blocking=False):
        return
    from django.db import close_old_connections

    close_old_connections()
    try:
        from business.expiry_notifications import (
            send_expiry_notifications_for_all_companies,
        )

        result = send_expiry_notifications_for_all_companies()
        client = int(result.get("client_emails_sent") or 0)
        team = int(result.get("team_digests_sent") or 0)
        logs = result.get("logs") or []
        if client or team:
            logger.warning(
                "Automatic expiry mail: %s client batch(es), %s team digest(s).",
                client,
                team,
            )
        for line in logs:
            lower = line.lower()
            if "failed" in lower or client or team:
                logger.warning("Automatic expiry mail: %s", line)
    except Exception:
        logger.exception("Local expiry mail scheduler tick failed")
    try:
        from business.team_notifications import scan_scheduled_notifications

        scan_scheduled_notifications()
    except Exception:
        logger.exception("Local team notification scan failed")
    try:
        from business.helpdesk_inbox import fetch_helpdesk_mail_for_all_companies

        result = fetch_helpdesk_mail_for_all_companies()
        created = int(result.get("created") or 0)
        if created:
            logger.warning("Automatic support mail: imported %s ticket(s).", created)
        for line in result.get("logs") or []:
            lower = str(line).lower()
            if "fail" in lower or "error" in lower:
                logger.warning("Automatic support mail: %s", line)
    except Exception:
        logger.exception("Local support mail fetch failed")
    finally:
        close_old_connections()
        _tick_lock.release()
