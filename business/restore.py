"""
Restore combined backups from the web UI or helpers.

Modes:
  full  — pg_restore --clean (used when the logged-in user has no company).
  merge — keep extra live columns, add dump-only columns/tables/rows, overwrite
          overlapping primary-key rows (used when a company profile exists).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any, Iterable

from django.conf import settings
from django.utils import timezone

from .backup import BackupError, backup_root

logger = logging.getLogger(__name__)

_SKIP_MERGE_TABLES = frozenset({"django_session"})
_INSERT_MISSING_TABLES = frozenset({"django_migrations"})
_ERROR_LINE = re.compile(r"(pg_restore:\s*error\b|\bERROR:)", re.IGNORECASE)
_SAFE_STAMP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


@dataclass
class RestoreResult:
    stamp: str
    mode: str
    directory: str = ""
    db: bool = False
    media: bool = False
    tables_merged: int = 0
    columns_added: list[str] = field(default_factory=list)
    tables_created: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_safe_stamp(stamp: str) -> bool:
    stamp = (stamp or "").strip()
    if not stamp or stamp in {".", "..", "exports"}:
        return False
    if "/" in stamp or "\\" in stamp or ".." in stamp:
        return False
    return bool(_SAFE_STAMP.match(stamp))


def last_restore_path() -> Path:
    return backup_root() / "last_restore.json"


def write_last_restore(result: RestoreResult) -> None:
    payload = result.to_dict()
    payload["id"] = uuid.uuid4().hex
    payload["finished_at"] = timezone.now().isoformat()
    try:
        last_restore_path().write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        logger.exception("Could not write last_restore.json")


def read_last_restore(*, max_age_hours: float = 12) -> dict[str, Any] | None:
    path = last_restore_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("finished_at") or ""
    if raw:
        try:
            finished = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=dt_timezone.utc)
            age = timezone.now() - finished
            if age.total_seconds() > max_age_hours * 3600:
                return None
        except (TypeError, ValueError):
            pass
    return data


def format_restore_message(result: RestoreResult | dict[str, Any]) -> str:
    data = result.to_dict() if isinstance(result, RestoreResult) else result
    stamp = data.get("stamp") or ""
    status = data.get("status") or "unknown"
    mode = data.get("mode") or ""
    if status == "ok":
        lines = [f"Restore completed from {stamp} ({mode})."]
    elif status == "partial":
        lines = [f"Restore from {stamp} completed with errors."]
    else:
        lines = [f"Restore from {stamp} failed."]
    added = data.get("columns_added") or []
    if added:
        shown = ", ".join(added[:25])
        extra = f" (+{len(added) - 25} more)" if len(added) > 25 else ""
        lines.append(f"Added columns: {shown}{extra}")
    created = data.get("tables_created") or []
    if created:
        lines.append("Added tables: " + ", ".join(created[:25]))
    for err in data.get("errors") or []:
        lines.append(f"Error: {err}")
    for warn in data.get("warnings") or []:
        lines.append(f"Note: {warn}")
    return "\n".join(lines)


def classify_pg_restore_output(returncode: int, stderr: str) -> tuple[bool, list[str]]:
    """
    Return (hard_failure, messages).

    pg_restore often exits 1 for warnings (missing roles, etc.). Only treat
    lines that look like real ERROR as a hard failure.
    """
    text = (stderr or "").strip()
    if returncode == 0:
        return False, []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    hard = [ln for ln in lines if _ERROR_LINE.search(ln)]
    if hard:
        return True, hard[:20]
    if lines:
        return False, lines[:12]
    return True, [f"pg_restore failed with exit code {returncode} and no error text."]


def build_upsert_sql(
    table: str,
    columns: list[str],
    pk_cols: list[str],
    *,
    schema: str = "public",
    has_identity: bool = False,
) -> str:
    """Return SQL for tests/helpers. Runtime uses psycopg.sql identifiers."""
    from psycopg import sql

    col_ids = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
    table_id = sql.Identifier(schema, table)
    stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        table_id, col_ids, placeholders
    )
    if has_identity:
        stmt = sql.SQL("INSERT INTO {} ({}) OVERRIDING SYSTEM VALUE VALUES ({})").format(
            table_id, col_ids, placeholders
        )
    if pk_cols:
        conflict = sql.SQL(", ").join(sql.Identifier(c) for c in pk_cols)
        update_cols = [c for c in columns if c not in pk_cols]
        if update_cols:
            assignments = sql.SQL(", ").join(
                sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
                for c in update_cols
            )
            stmt = stmt + sql.SQL(" ON CONFLICT ({}) DO UPDATE SET {}").format(
                conflict, assignments
            )
        else:
            stmt = stmt + sql.SQL(" ON CONFLICT ({}) DO NOTHING").format(conflict)
    return stmt.as_string(None)


def restore_from_stamp(
    stamp: str,
    *,
    merge: bool,
    restore_db: bool = True,
    restore_media: bool = True,
    root: Path | None = None,
) -> RestoreResult:
    if not is_safe_stamp(stamp):
        raise BackupError("Invalid backup id.")

    root = root or backup_root()
    dest_dir = root / stamp
    result = RestoreResult(
        stamp=stamp,
        mode="merge" if merge else "full",
        directory=str(dest_dir),
    )
    if not dest_dir.is_dir():
        result.status = "failed"
        result.errors.append(f"Backup not found: {stamp}")
        write_last_restore(result)
        raise BackupError(result.errors[0])

    try:
        if restore_db:
            dump = dest_dir / "db.dump"
            if not dump.is_file():
                raise BackupError(f"Missing db.dump in {dest_dir}")
            if merge:
                _restore_db_merge(dump, result)
            else:
                _restore_db_full(dump, result)

        if restore_media:
            archive = dest_dir / "media.tar.gz"
            if not archive.is_file():
                result.warnings.append(
                    f"Media archive missing in this backup ({stamp}); database restore continued without files."
                )
            else:
                _restore_media(archive, result)

        _run_migrate(result)
        _finalize_status(result)
        write_last_restore(result)
        if result.status == "failed":
            raise BackupError(format_restore_message(result))
        return result
    except BackupError as exc:
        if str(exc) not in result.errors:
            result.errors.append(str(exc))
        _finalize_status(result)
        write_last_restore(result)
        raise
    except Exception as exc:
        result.errors.append(f"Unexpected restore error: {exc}")
        result.status = "failed"
        write_last_restore(result)
        logger.exception("Restore failed stamp=%s mode=%s", stamp, result.mode)
        raise BackupError(format_restore_message(result)) from exc


def classify_backup_filename(filename: str) -> str:
    """Return db, media, zip, manifest, or unknown for a downloaded backup file."""
    name = Path(filename or "").name.lower().strip()
    if not name:
        return "unknown"
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(".json"):
        return "manifest"
    if name.endswith(".dump") or name.endswith(".backup") or name == "db.dump":
        return "db"
    if (
        name.endswith(".tar.gz")
        or name.endswith(".tgz")
        or name.endswith(".tar")
        or name.endswith(".gz")
        or name == "media.tar.gz"
    ):
        return "media"
    return "unknown"


def restore_from_uploaded_files(
    saved_paths: list[Path],
    *,
    merge: bool,
) -> RestoreResult:
    """
    Restore from files the user uploaded (downloaded dump/media/zip).

    saved_paths are already on disk. A working folder is created under
    BACKUP_DIR/uploads/, then restore_from_stamp runs against it.
    """
    if not saved_paths:
        raise BackupError("Choose at least one backup file to restore.")

    stamp = timezone.localtime().strftime("%Y%m%d_%H%M%S") + "_upload"
    work_root = backup_root() / "uploads"
    work_root.mkdir(parents=True, exist_ok=True)
    dest_dir = work_root / stamp
    dest_dir.mkdir(parents=True, exist_ok=False)

    try:
        dump_dest = dest_dir / "db.dump"
        media_dest = dest_dir / "media.tar.gz"
        has_db = False
        has_media = False
        extras: list[str] = []

        for path in saved_paths:
            kind = classify_backup_filename(path.name)
            if kind == "zip":
                db_found, media_found = _extract_backup_zip(path, dest_dir)
                has_db = has_db or db_found
                has_media = has_media or media_found
                continue
            if kind == "db":
                if has_db:
                    extras.append(path.name)
                    continue
                shutil.copy2(path, dump_dest)
                has_db = True
                continue
            if kind == "media":
                if has_media:
                    extras.append(path.name)
                    continue
                shutil.copy2(path, media_dest)
                has_media = True
                continue
            if kind == "manifest":
                shutil.copy2(path, dest_dir / "manifest.json")
                continue
            extras.append(path.name)

        if extras:
            logger.warning("Ignored restore upload files: %s", extras)
        if not has_db and not has_media:
            raise BackupError(
                "Could not recognise a database dump or media archive in the uploaded files. "
                "Upload the .dump and/or .tar.gz you downloaded from Backups "
                "(or a .zip that contains them)."
            )
        if not dump_dest.is_file():
            has_db = False
        if not media_dest.is_file():
            has_media = False

        result = restore_from_stamp(
            stamp,
            merge=merge,
            restore_db=has_db,
            restore_media=has_media,
            root=work_root,
        )
        if extras:
            result.warnings.append(
                "These files were not used: " + ", ".join(extras[:8])
            )
        return result
    finally:
        shutil.rmtree(dest_dir, ignore_errors=True)


def _extract_backup_zip(zip_path: Path, dest_dir: Path) -> tuple[bool, bool]:
    has_db = False
    has_media = False
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > 40:
                raise BackupError("Zip has too many files to restore safely.")
            uncompressed = sum(max(i.file_size, 0) for i in infos)
            max_bytes = int(getattr(settings, "BACKUP_UPLOAD_MAX_BYTES", 0) or 0)
            if max_bytes and uncompressed > max_bytes:
                raise BackupError("Zip is larger than the restore upload limit.")
            for info in infos:
                name = Path(info.filename.replace("\\", "/")).name
                if not name or name in {".", ".."} or "/" in name or "\\" in name:
                    continue
                kind = classify_backup_filename(name)
                if kind == "db" and not has_db:
                    target = dest_dir / "db.dump"
                elif kind == "media" and not has_media:
                    target = dest_dir / "media.tar.gz"
                elif kind == "manifest":
                    target = dest_dir / "manifest.json"
                else:
                    continue
                with zf.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
                if kind == "db":
                    has_db = True
                elif kind == "media":
                    has_media = True
    except BackupError:
        raise
    except zipfile.BadZipFile as exc:
        raise BackupError(f"The zip file could not be read: {exc}") from exc
    except OSError as exc:
        raise BackupError(f"Could not extract the zip file: {exc}") from exc
    return has_db, has_media


def _finalize_status(result: RestoreResult) -> None:
    if result.errors and (result.db or result.media or result.tables_merged):
        result.status = "partial"
    elif result.errors:
        result.status = "failed"
    else:
        result.status = "ok"


def _run_migrate(result: RestoreResult) -> None:
    try:
        from django.core.management import call_command
        from django.db import connections

        connections.close_all()
        call_command("migrate", interactive=False, verbosity=0)
    except Exception as exc:
        result.warnings.append(f"Could not apply database migrations after restore: {exc}")
        logger.exception("migrate after restore failed")


def _pg_restore_binary() -> str:
    configured = (getattr(settings, "BACKUP_PG_RESTORE", "") or "").strip()
    if configured:
        return configured
    found = shutil.which("pg_restore")
    if found:
        return found
    raise BackupError(
        "pg_restore not found. Install PostgreSQL client tools "
        "or set BACKUP_PG_RESTORE to the full path of pg_restore."
    )


def _pg_env() -> tuple[dict[str, str], dict[str, str]]:
    from . import backup as backup_lib

    db = backup_lib._db_settings()
    env = os.environ.copy()
    if db["PASSWORD"]:
        env["PGPASSWORD"] = db["PASSWORD"]
    return db, env


def _run_pg_restore(dump: Path, dbname: str, *, clean: bool) -> tuple[int, str]:
    db, env = _pg_env()
    binary = _pg_restore_binary()
    cmd = [
        binary,
        "-h",
        db["HOST"],
        "-p",
        db["PORT"],
        "-U",
        db["USER"],
        "-d",
        dbname,
        "--no-owner",
        "--no-acl",
    ]
    if clean:
        cmd.extend(["--clean", "--if-exists"])
    cmd.append(str(dump))
    logger.warning("Running pg_restore db=%s dump=%s clean=%s", dbname, dump, clean)
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
        raise BackupError(f"pg_restore executable not found: {binary}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError("pg_restore timed out after waiting too long.") from exc
    stderr = (completed.stderr or completed.stdout or "").strip()
    return completed.returncode, stderr


def _restore_db_full(dump: Path, result: RestoreResult) -> None:
    from . import backup as backup_lib

    db = backup_lib._db_settings()
    code, stderr = _run_pg_restore(dump, db["NAME"], clean=True)
    hard, messages = classify_pg_restore_output(code, stderr)
    if hard:
        detail = "; ".join(messages)[:2000]
        raise BackupError(f"Database restore failed: {detail}")
    result.db = True
    if messages:
        result.warnings.extend(messages[:8])


def _restore_media(archive: Path, result: RestoreResult) -> None:
    media_root = Path(settings.MEDIA_ROOT)
    parent = media_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    logger.warning("Restoring media into %s", media_root)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            _safe_extract_tar(tar, parent)
    except tarfile.TarError as exc:
        raise BackupError(f"Media archive could not be read: {exc}") from exc
    except OSError as exc:
        raise BackupError(f"Media restore failed: {exc}") from exc
    result.media = True


def _safe_extract_tar(tar: tarfile.TarFile, dest: Path) -> None:
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        member_path = (dest / member.name).resolve()
        try:
            member_path.relative_to(dest_resolved)
        except ValueError as exc:
            raise BackupError(
                f"Unsafe path in media archive: {member.name}"
            ) from exc
    kwargs: dict[str, Any] = {}
    import sys

    if sys.version_info >= (3, 12):
        kwargs["filter"] = "data"
    tar.extractall(path=dest, **kwargs)


def _restore_db_merge(dump: Path, result: RestoreResult) -> None:
    import psycopg

    from . import backup as backup_lib

    db = backup_lib._db_settings()
    temp_name = _temp_db_name(result.stamp)
    admin = None
    src = None
    dest = None
    try:
        admin = _admin_connect()
        _create_database(admin, temp_name)
        code, stderr = _run_pg_restore(dump, temp_name, clean=True)
        hard, messages = classify_pg_restore_output(code, stderr)
        if hard:
            detail = "; ".join(messages)[:2000]
            raise BackupError(
                f"Could not load backup into a temporary database: {detail}"
            )
        if messages:
            result.warnings.extend(messages[:6])

        src = _connect_db(temp_name)
        dest = _connect_db(db["NAME"])
        dest.autocommit = True
        src.autocommit = True
        _merge_databases(src, dest, result)
        result.db = True
    finally:
        for conn in (src, dest):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        if admin is not None:
            try:
                _drop_database(admin, temp_name)
            except Exception as exc:
                result.warnings.append(
                    f"Temporary restore database {temp_name} could not be dropped: {exc}"
                )
            try:
                admin.close()
            except Exception:
                pass


def _temp_db_name(stamp: str) -> str:
    suffix = secrets.token_hex(4)
    raw = f"sales_rst_{stamp}_{suffix}".lower()
    cleaned = re.sub(r"[^a-z0-9_]", "_", raw)
    return cleaned[:63]


def _connect_kwargs(dbname: str) -> dict[str, Any]:
    from . import backup as backup_lib

    db = backup_lib._db_settings()
    return {
        "host": db["HOST"],
        "port": db["PORT"],
        "user": db["USER"],
        "password": db["PASSWORD"],
        "dbname": dbname,
    }


def _adapt_row(row: tuple[Any, ...], col_types: list[str]) -> tuple[Any, ...]:
    """Wrap JSON dict/list values so psycopg 3 can insert them into json/jsonb."""
    from psycopg.types.json import Json, Jsonb

    adapted: list[Any] = []
    for value, typ in zip(row, col_types):
        if value is None:
            adapted.append(None)
            continue
        kind = _json_kind(typ)
        if kind and isinstance(value, (dict, list)):
            adapted.append(Jsonb(value) if kind == "jsonb" else Json(value))
        else:
            adapted.append(value)
    return tuple(adapted)


def _json_kind(typ: str) -> str | None:
    t = (typ or "").lower().strip()
    if t == "jsonb":
        return "jsonb"
    if t == "json":
        return "json"
    return None


def _connect_db(dbname: str):
    import psycopg

    try:
        conn = psycopg.connect(**_connect_kwargs(dbname))
        conn.autocommit = True
        return conn
    except Exception as exc:
        raise BackupError(f"Could not connect to database {dbname}: {exc}") from exc


def _admin_connect():
    import psycopg

    from . import backup as backup_lib

    db = backup_lib._db_settings()
    last_exc: Exception | None = None
    for name in ("postgres", "template1", db["NAME"]):
        try:
            conn = psycopg.connect(**_connect_kwargs(name))
            conn.autocommit = True
            return conn
        except Exception as exc:
            last_exc = exc
    raise BackupError(
        "Could not open a PostgreSQL admin connection to create a temporary "
        f"restore database: {last_exc}"
    )


def _create_database(admin, name: str) -> None:
    from psycopg import sql

    try:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    except Exception as exc:
        raise BackupError(
            "Merge restore needs permission to CREATE DATABASE on PostgreSQL. "
            f"The server returned: {exc}"
        ) from exc


def _drop_database(admin, name: str) -> None:
    from psycopg import sql

    admin.execute(
        """
        SELECT pg_terminate_backend(pid)
        FROM pg_stat_activity
        WHERE datname = %s AND pid <> pg_backend_pid()
        """,
        (name,),
    )
    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def _merge_databases(src, dest, result: RestoreResult) -> None:
    replication = _try_replica_role(dest)
    try:
        src_tables = _list_tables(src)
        dest_tables = set(_list_tables(dest))
        pending_create = [t for t in src_tables if t not in dest_tables]
        for table in pending_create:
            try:
                _create_table_from_src(src, dest, table, result)
            except Exception as exc:
                result.errors.append(
                    f"Could not add table {table}: {exc}"
                )

        dest_tables = set(_list_tables(dest))
        pending = [t for t in src_tables if t in dest_tables]
        errors_by_table: dict[str, str] = {}
        for _round in range(8):
            if not pending:
                break
            still: list[str] = []
            for table in pending:
                try:
                    _merge_table(src, dest, table, result)
                except Exception as exc:
                    errors_by_table[table] = str(exc)
                    still.append(table)
                    logger.warning("Merge table %s failed (will retry): %s", table, exc)
            if len(still) == len(pending):
                break
            pending = still
        for table in pending:
            result.errors.append(
                f"Table {table} could not be restored: {errors_by_table.get(table, 'unknown error')}"
            )
    finally:
        if replication:
            _try_origin_role(dest)


def _try_replica_role(conn) -> bool:
    try:
        conn.execute("SET session_replication_role = replica")
        return True
    except Exception:
        return False


def _try_origin_role(conn) -> None:
    try:
        conn.execute("SET session_replication_role = origin")
    except Exception:
        pass


def _list_tables(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r'
              AND n.nspname = 'public'
            ORDER BY c.relname
            """
        )
        return [row[0] for row in cur.fetchall()]


def _column_info(conn, table: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              a.attname,
              pg_catalog.format_type(a.atttypid, a.atttypmod) AS typ,
              COALESCE(a.attidentity, '') AS identity
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = %s
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            (table,),
        )
        return [
            {"name": row[0], "type": row[1], "identity": row[2] or ""}
            for row in cur.fetchall()
        ]


def _primary_key_cols(conn, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a
              ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = %s
              AND i.indisprimary
            ORDER BY array_position(i.indkey, a.attnum)
            """,
            (table,),
        )
        cols = [row[0] for row in cur.fetchall()]
        if cols:
            return cols
        cur.execute(
            """
            SELECT i.indexrelid
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = %s
              AND i.indisunique
              AND NOT i.indisprimary
            ORDER BY i.indexrelid
            LIMIT 1
            """,
            (table,),
        )
        idx = cur.fetchone()
        if not idx:
            return []
        cur.execute(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a
              ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
            WHERE i.indexrelid = %s
            ORDER BY array_position(i.indkey, a.attnum)
            """,
            (idx[0],),
        )
        return [row[0] for row in cur.fetchall()]


def _current_dbname(conn) -> str:
    dbname = getattr(getattr(conn, "info", None), "dbname", None)
    if dbname:
        return dbname
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        return cur.fetchone()[0]


def _psql_binary() -> str:
    found = shutil.which("psql")
    if found:
        return found
    restore_bin = (getattr(settings, "BACKUP_PG_RESTORE", "") or "").strip()
    if restore_bin:
        candidate = Path(restore_bin).with_name("psql")
        if candidate.is_file():
            return str(candidate)
        windows = candidate.with_suffix(".exe")
        if windows.is_file():
            return str(windows)
    raise BackupError(
        "psql not found. Install PostgreSQL client tools so restore can add new tables."
    )


def _apply_sql(dbname: str, sql_text: str) -> None:
    db, env = _pg_env()
    cmd = [
        _psql_binary(),
        "-h",
        db["HOST"],
        "-p",
        db["PORT"],
        "-U",
        db["USER"],
        "-d",
        dbname,
        "-v",
        "ON_ERROR_STOP=1",
        "-1",
    ]
    completed = subprocess.run(
        cmd,
        input=sql_text,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        raise BackupError(err[:1500] or "psql failed while applying table schema")


def _create_table_from_src(src, dest, table: str, result: RestoreResult) -> None:
    from . import backup as backup_lib

    db, env = _pg_env()
    dump_bin = backup_lib._pg_dump_binary()
    src_db = _current_dbname(src)
    dest_db = _current_dbname(dest)
    cmd = [
        dump_bin,
        "-h",
        db["HOST"],
        "-p",
        db["PORT"],
        "-U",
        db["USER"],
        "-d",
        src_db,
        "-t",
        f"public.{table}",
        "--schema-only",
        "--no-owner",
        "--no-acl",
    ]
    completed = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        raise BackupError(err[:1500] or f"pg_dump schema for {table} failed")
    sql_text = completed.stdout or ""
    if not sql_text.strip():
        raise BackupError(f"No schema SQL returned for table {table}")
    _apply_sql(dest_db, sql_text)
    result.tables_created.append(table)


def _merge_table(src, dest, table: str, result: RestoreResult) -> None:
    from psycopg import sql

    if table in _SKIP_MERGE_TABLES:
        return

    src_cols = _column_info(src, table)
    dest_cols = _column_info(dest, table)
    dest_names = {c["name"] for c in dest_cols}
    src_by_name = {c["name"]: c for c in src_cols}

    for col in src_cols:
        if col["name"] in dest_names:
            continue
        try:
            dest.execute(
                sql.SQL("ALTER TABLE {} ADD COLUMN {} {}").format(
                    sql.Identifier("public", table),
                    sql.Identifier(col["name"]),
                    sql.SQL(col["type"]),
                )
            )
            label = f"{table}.{col['name']}"
            result.columns_added.append(label)
            dest_names.add(col["name"])
        except Exception as exc:
            result.errors.append(
                f"Could not add column {table}.{col['name']}: {exc}"
            )

    copy_cols = [c["name"] for c in src_cols if c["name"] in dest_names]
    if not copy_cols:
        raise BackupError(f"No overlapping columns for table {table}")

    if table in _INSERT_MISSING_TABLES:
        _insert_missing_rows(src, dest, table, copy_cols)
        result.tables_merged += 1
        return

    pk_cols = [c for c in _primary_key_cols(dest, table) if c in copy_cols]
    has_identity = any(
        c.get("identity") for c in dest_cols if c["name"] in copy_cols
    ) or any(src_by_name.get(name, {}).get("identity") for name in copy_cols)
    insert_sql = _upsert_query(table, copy_cols, pk_cols, has_identity=has_identity)
    type_by_name = {c["name"]: c.get("type") or "" for c in src_cols}
    col_types = [type_by_name.get(name, "") for name in copy_cols]
    _copy_rows(
        src, dest, table, copy_cols, insert_sql, pk_cols, result, col_types=col_types
    )
    _sync_sequences(dest, table, copy_cols)
    result.tables_merged += 1


def _upsert_query(
    table: str,
    columns: list[str],
    pk_cols: list[str],
    *,
    has_identity: bool,
):
    from psycopg import sql

    col_ids = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
    table_id = sql.Identifier("public", table)
    if has_identity:
        stmt = sql.SQL(
            "INSERT INTO {} ({}) OVERRIDING SYSTEM VALUE VALUES ({})"
        ).format(table_id, col_ids, placeholders)
    else:
        stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            table_id, col_ids, placeholders
        )
    if pk_cols:
        conflict = sql.SQL(", ").join(sql.Identifier(c) for c in pk_cols)
        update_cols = [c for c in columns if c not in pk_cols]
        if update_cols:
            assignments = sql.SQL(", ").join(
                sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
                for c in update_cols
            )
            stmt = stmt + sql.SQL(" ON CONFLICT ({}) DO UPDATE SET {}").format(
                conflict, assignments
            )
        else:
            stmt = stmt + sql.SQL(" ON CONFLICT ({}) DO NOTHING").format(conflict)
    return stmt


def _copy_rows(
    src,
    dest,
    table: str,
    columns: list[str],
    insert_sql,
    pk_cols: list[str],
    result: RestoreResult,
    *,
    col_types: list[str] | None = None,
) -> None:
    from psycopg import sql

    types = col_types or [""] * len(columns)
    select = sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        sql.Identifier("public", table),
    )
    with src.cursor() as cur:
        cur.execute(select)
        failed_rows = 0
        last_exc: Exception | None = None
        while True:
            rows = cur.fetchmany(400)
            if not rows:
                break
            adapted_rows = [_adapt_row(tuple(row), types) for row in rows]
            try:
                with dest.cursor() as dcur:
                    dcur.executemany(insert_sql, adapted_rows)
            except Exception:
                for row in adapted_rows:
                    try:
                        with dest.cursor() as dcur:
                            dcur.execute(insert_sql, row)
                    except Exception as row_exc:
                        failed_rows += 1
                        last_exc = row_exc
        if failed_rows:
            detail = f" ({last_exc})" if last_exc else ""
            raise BackupError(
                f"{table}: {failed_rows} row(s) could not be restored{detail}"
            )


def _insert_missing_rows(src, dest, table: str, columns: list[str]) -> None:
    from psycopg import sql

    key_cols = [c for c in ("app", "name") if c in columns]
    if table == "django_migrations" and len(key_cols) == 2:
        existing: set[tuple[Any, ...]] = set()
        with dest.cursor() as cur:
            cur.execute("SELECT app, name FROM django_migrations")
            existing = {(row[0], row[1]) for row in cur.fetchall()}
        insert_cols = [c for c in columns if c != "id"]
        insert_sql = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier("public", table),
            sql.SQL(", ").join(sql.Identifier(c) for c in insert_cols),
            sql.SQL(", ").join(sql.Placeholder() for _ in insert_cols),
        )
        with src.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT {} FROM {}").format(
                    sql.SQL(", ").join(sql.Identifier(c) for c in insert_cols),
                    sql.Identifier("public", table),
                )
            )
            rows = cur.fetchall()
        with dest.cursor() as dcur:
            for row in rows:
                mapping = dict(zip(insert_cols, row))
                if (mapping.get("app"), mapping.get("name")) in existing:
                    continue
                dcur.execute(insert_sql, row)
        return

    select = sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        sql.Identifier("public", table),
    )
    insert_sql = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
        sql.Identifier("public", table),
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )
    with src.cursor() as cur:
        cur.execute(select)
        rows = cur.fetchall()
    with dest.cursor() as dcur:
        if rows:
            dcur.executemany(insert_sql, rows)


def _sync_sequences(conn, table: str, columns: Iterable[str]) -> None:
    from psycopg import sql

    for col in columns:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (f"public.{table}", col))
                row = cur.fetchone()
                seq = row[0] if row else None
                if not seq:
                    continue
                cur.execute(
                    sql.SQL("SELECT COALESCE(MAX({}), 1) FROM {}").format(
                        sql.Identifier(col),
                        sql.Identifier("public", table),
                    )
                )
                max_id = cur.fetchone()[0]
                if max_id is None:
                    continue
                cur.execute("SELECT setval(%s, %s, true)", (seq, int(max_id)))
        except Exception:
            continue


def backups_for_ui() -> list[dict[str, Any]]:
    from .backup import list_backups

    items: list[dict[str, Any]] = []
    for raw in list_backups():
        man = raw.get("manifest") or {}
        db_bytes = (man.get("database") or {}).get("bytes") or 0
        media_bytes = (man.get("media") or {}).get("bytes") or 0
        items.append(
            {
                **raw,
                "status": man.get("status") or ("ok" if raw.get("has_db") else "unknown"),
                "created_at": man.get("created_at") or "",
                "db_label": _format_bytes(db_bytes) if raw.get("has_db") else "—",
                "media_label": _format_bytes(media_bytes) if raw.get("has_media") else "—",
                "errors": man.get("errors") or [],
                "can_restore": bool(raw.get("has_db")),
            }
        )
    return items


def _format_bytes(n: int) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"
