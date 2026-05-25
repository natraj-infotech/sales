"""
Session-backed staging for spreadsheet imports.

Uses the user's Django session for metadata and a temp file on disk for bytes so
imports work behind multiple Gunicorn/uwsgi workers (LocMemCache is per-process).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings

IMPORT_TMP_DIR = settings.BASE_DIR / ".import_tmp"


@dataclass(frozen=True)
class ImportStashConfig:
    """Keys and filename prefix for one import flow (subscription, product, …)."""

    prefix: str
    session_token_key: str
    session_meta_key: str
    legacy_filename_pattern: str | None = None  # e.g. "{user_id}_{token}.upload"


SUBSCRIPTION_IMPORT_STASH = ImportStashConfig(
    prefix="sub",
    session_token_key="subscription_import_token",
    session_meta_key="subscription_import_meta",
    legacy_filename_pattern="{user_id}_{token}.upload",
)

PRODUCT_IMPORT_STASH = ImportStashConfig(
    prefix="product",
    session_token_key="product_import_token",
    session_meta_key="product_import_meta",
)


class ImportStash:
    def __init__(self, config: ImportStashConfig) -> None:
        self.config = config

    def upload_path(self, user_id: int, token: str) -> Path:
        return IMPORT_TMP_DIR / f"{self.config.prefix}_{user_id}_{token}.upload"

    def _legacy_paths(self, user_id: int, token: str) -> list[Path]:
        if not self.config.legacy_filename_pattern:
            return []
        return [
            IMPORT_TMP_DIR
            / self.config.legacy_filename_pattern.format(
                user_id=user_id, token=token
            )
        ]

    def resolve_upload_path(self, user_id: int, token: str) -> Path | None:
        path = self.upload_path(user_id, token)
        if path.is_file():
            return path
        for legacy in self._legacy_paths(user_id, token):
            if legacy.is_file():
                return legacy
        return None

    def stash(self, request, file_bytes: bytes, meta: dict[str, Any]) -> str:
        IMPORT_TMP_DIR.mkdir(parents=True, exist_ok=True)
        token = str(uuid.uuid4())
        file_path = self.upload_path(request.user.pk, token)
        file_path.write_bytes(file_bytes)
        request.session[self.config.session_token_key] = token
        request.session[self.config.session_meta_key] = meta
        request.session.modified = True
        return token

    def get(self, request) -> tuple[str, dict[str, Any]] | tuple[None, None]:
        token = request.session.get(self.config.session_token_key)
        if not token:
            return None, None
        meta = request.session.get(self.config.session_meta_key)
        file_path = self.resolve_upload_path(request.user.pk, token)
        if not file_path or not meta:
            return None, None
        data = {**meta, "path": str(file_path)}
        if not self.read_bytes(data):
            return None, None
        return token, data

    @staticmethod
    def read_bytes(data: dict[str, Any]) -> bytes:
        path = data.get("path")
        if path:
            file_path = Path(path)
            if file_path.is_file():
                return file_path.read_bytes()
        legacy = data.get("bytes")
        if isinstance(legacy, (bytes, bytearray)) and legacy:
            return bytes(legacy)
        return b""

    def clear(self, request, token: str) -> None:
        path = self.resolve_upload_path(request.user.pk, token)
        if path:
            path.unlink(missing_ok=True)
        for legacy in self._legacy_paths(request.user.pk, token):
            legacy.unlink(missing_ok=True)
        request.session.pop(self.config.session_token_key, None)
        request.session.pop(self.config.session_meta_key, None)
        request.session.modified = True


subscription_import_stash = ImportStash(SUBSCRIPTION_IMPORT_STASH)
product_import_stash = ImportStash(PRODUCT_IMPORT_STASH)
