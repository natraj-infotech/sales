"""
Combined sales backups: PostgreSQL dump + media archive + manifest.

One run creates:
  BACKUP_DIR/<stamp>/
    db.dump          (pg_dump -Fc)
    media.tar.gz     (MEDIA_ROOT)
    manifest.json

Retention deletes older stamp folders beyond BACKUP_RETENTION_DAYS.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tarfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class BackupError(Exception):
    pass


@dataclass
class BackupResult:
    stamp: str
    directory: str
    db_path: str = ""
    media_path: str = ""
    manifest_path: str = ""
    db_bytes: int = 0
    media_bytes: int = 0
    status: str = "ok"
    errors: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def backup_root() -> Path:
    raw = (getattr(settings, "BACKUP_DIR", "") or "").strip()
    if raw:
        path = Path(raw)
    else:
        path = Path(settings.BASE_DIR) / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def retention_days() -> int:
    try:
        return max(1, int(getattr(settings, "BACKUP_RETENTION_DAYS", 14) or 14))
    except (TypeError, ValueError):
        return 14


def backups_enabled() -> bool:
    return bool(getattr(settings, "BACKUP_ENABLED", True))


def _pg_dump_binary() -> str:
    configured = (getattr(settings, "BACKUP_PG_DUMP", "") or "").strip()
    if configured:
        return configured
    found = shutil.which("pg_dump")
    if found:
        return found
    raise BackupError(
        "pg_dump not found on PATH. Install PostgreSQL client tools "
        "or set BACKUP_PG_DUMP to the full path of pg_dump."
    )


def _db_settings() -> dict[str, str]:
    db = settings.DATABASES["default"]
    engine = (db.get("ENGINE") or "")
    if "postgresql" not in engine:
        raise BackupError(
            f"Backups require PostgreSQL; current ENGINE={engine!r}"
        )
    return {
        "NAME": str(db.get("NAME") or ""),
        "USER": str(db.get("USER") or ""),
        "PASSWORD": str(db.get("PASSWORD") or ""),
        "HOST": str(db.get("HOST") or "localhost"),
        "PORT": str(db.get("PORT") or "5432"),
    }


def _run_pg_dump(dest: Path) -> None:
    db = _db_settings()
    binary = _pg_dump_binary()
    env = os.environ.copy()
    if db["PASSWORD"]:
        env["PGPASSWORD"] = db["PASSWORD"]
    cmd = [
        binary,
        "-h",
        db["HOST"],
        "-p",
        db["PORT"],
        "-U",
        db["USER"],
        "-d",
        db["NAME"],
        "-Fc",
        "-f",
        str(dest),
    ]
    logger.info("Running pg_dump → %s", dest)
    try:
        completed = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=int(getattr(settings, "BACKUP_PG_DUMP_TIMEOUT", 3600) or 3600),
            check=False,
        )
    except FileNotFoundError as exc:
        raise BackupError(f"pg_dump executable not found: {binary}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError("pg_dump timed out") from exc

    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        raise BackupError(f"pg_dump failed ({completed.returncode}): {err[:2000]}")
    if not dest.is_file() or dest.stat().st_size <= 0:
        raise BackupError("pg_dump produced an empty dump file")


def create_db_dump_export() -> tuple[Path, str]:
    """
    Write a fresh pg_dump -Fc file under BACKUP_DIR/exports/ for download.

    Returns (path, download_filename).
    """
    if not backups_enabled():
        raise BackupError("Backups are disabled (BACKUP_ENABLED=false)")
    stamp = timezone.localtime().strftime("%Y%m%d_%H%M%S")
    exports = backup_root() / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    db = _db_settings()
    filename = f"{db['NAME'] or 'sales'}_{stamp}.dump"
    dest = exports / filename
    _run_pg_dump(dest)
    # Keep only the newest N export dumps to avoid filling disk.
    keep = 10
    dumps = sorted(exports.glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in dumps[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest, filename


def _archive_media(dest: Path) -> int:
    media_root = Path(settings.MEDIA_ROOT)
    if not media_root.exists():
        # Empty archive still records that media was considered.
        with tarfile.open(dest, "w:gz") as tar:
            pass
        return dest.stat().st_size

    logger.info("Archiving media %s → %s", media_root, dest)
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(str(media_root), arcname="media")
    return dest.stat().st_size


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(settings.BASE_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode == 0:
            return (completed.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def prune_old_backups(root: Path | None = None, *, keep_days: int | None = None) -> list[str]:
    """Delete backup stamp directories older than retention. Returns pruned names."""
    root = root or backup_root()
    days = keep_days if keep_days is not None else retention_days()
    cutoff = timezone.now() - timedelta(days=days)
    pruned: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in {"exports", "uploads"}:
            continue
        # Prefer manifest created_at; fall back to directory mtime.
        created = None
        manifest = child / "manifest.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                raw = data.get("created_at") or ""
                if raw:
                    created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if timezone.is_naive(created):
                        created = timezone.make_aware(created, timezone.get_current_timezone())
            except (OSError, ValueError, json.JSONDecodeError, TypeError):
                created = None
        if created is None:
            created = datetime.fromtimestamp(
                child.stat().st_mtime, tz=timezone.get_current_timezone()
            )
        if created < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            pruned.append(child.name)
            logger.info("Pruned old backup %s", child)
    return pruned


def list_backups(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or backup_root()
    items: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), reverse=True):
        if not child.is_dir() or child.name in {"exports", "uploads"}:
            continue
        manifest_path = child / "manifest.json"
        info: dict[str, Any] = {
            "stamp": child.name,
            "directory": str(child),
            "has_db": (child / "db.dump").is_file(),
            "has_media": (child / "media.tar.gz").is_file(),
        }
        if manifest_path.is_file():
            try:
                info["manifest"] = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                info["manifest"] = None
        items.append(info)
    return items


def create_backup(*, prune: bool = True) -> BackupResult:
    """
    Create a combined DB + media backup under BACKUP_DIR/<stamp>/.
    """
    if not backups_enabled():
        raise BackupError("Backups are disabled (BACKUP_ENABLED=false)")

    stamp = timezone.localtime().strftime("%Y%m%d_%H%M%S")
    root = backup_root()
    dest_dir = root / stamp
    dest_dir.mkdir(parents=True, exist_ok=False)

    result = BackupResult(stamp=stamp, directory=str(dest_dir))
    db_path = dest_dir / "db.dump"
    media_path = dest_dir / "media.tar.gz"
    manifest_path = dest_dir / "manifest.json"

    try:
        _run_pg_dump(db_path)
        result.db_path = str(db_path)
        result.db_bytes = db_path.stat().st_size

        result.media_bytes = _archive_media(media_path)
        result.media_path = str(media_path)

        db = _db_settings()
        manifest = {
            "created_at": timezone.now().isoformat(),
            "stamp": stamp,
            "status": "ok",
            "database": {
                "name": db["NAME"],
                "host": db["HOST"],
                "port": db["PORT"],
                "user": db["USER"],
                "dump_file": "db.dump",
                "bytes": result.db_bytes,
            },
            "media": {
                "root": str(settings.MEDIA_ROOT),
                "archive": "media.tar.gz",
                "bytes": result.media_bytes,
            },
            "app": {
                "base_dir": str(settings.BASE_DIR),
                "git_commit": _git_commit(),
            },
        }
        _write_manifest(manifest_path, manifest)
        result.manifest_path = str(manifest_path)

        if prune:
            result.pruned = prune_old_backups(root)

        logger.info(
            "Backup complete stamp=%s db_bytes=%s media_bytes=%s",
            stamp,
            result.db_bytes,
            result.media_bytes,
        )
        return result
    except Exception as exc:
        result.status = "failed"
        result.errors.append(str(exc))
        try:
            _write_manifest(
                manifest_path,
                {
                    "created_at": timezone.now().isoformat(),
                    "stamp": stamp,
                    "status": "failed",
                    "errors": result.errors,
                },
            )
            result.manifest_path = str(manifest_path)
        except OSError:
            pass
        logger.exception("Backup failed stamp=%s", stamp)
        raise BackupError(str(exc)) from exc


def restore_backup(
    stamp: str,
    *,
    restore_db: bool = True,
    restore_media: bool = True,
    root: Path | None = None,
    merge: bool = False,
) -> dict[str, Any]:
    """
    Restore from BACKUP_DIR/<stamp>/.

    merge=False uses pg_restore --clean (full replace).
    merge=True keeps extra live columns and upserts dump rows.
    """
    from business.restore import restore_from_stamp

    result = restore_from_stamp(
        stamp,
        merge=merge,
        restore_db=restore_db,
        restore_media=restore_media,
        root=root,
    )
    return result.to_dict()
