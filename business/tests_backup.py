"""Tests for combined backup helpers (no real pg_dump required)."""

import json
import tarfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from business.backup import (
    BackupError,
    create_backup,
    list_backups,
    prune_old_backups,
)


class BackupHelpersTests(SimpleTestCase):
    def test_prune_old_backups(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "20200101_000000"
            new = root / "20991231_000000"
            old.mkdir()
            new.mkdir()
            old_manifest = {
                "created_at": (timezone.now() - timedelta(days=30)).isoformat(),
                "status": "ok",
            }
            new_manifest = {
                "created_at": timezone.now().isoformat(),
                "status": "ok",
            }
            (old / "manifest.json").write_text(
                json.dumps(old_manifest), encoding="utf-8"
            )
            (new / "manifest.json").write_text(
                json.dumps(new_manifest), encoding="utf-8"
            )
            pruned = prune_old_backups(root, keep_days=14)
            self.assertIn("20200101_000000", pruned)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())

    @override_settings(BACKUP_ENABLED=True)
    def test_create_backup_writes_files(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            backup_dir = tmp_path / "backups"
            media_dir = tmp_path / "media"
            media_dir.mkdir()
            (media_dir / "logo.txt").write_text("x", encoding="utf-8")

            def fake_pg_dump(dest: Path):
                dest.write_bytes(b"FAKE DUMP")

            with override_settings(
                BACKUP_DIR=str(backup_dir),
                MEDIA_ROOT=str(media_dir),
                BACKUP_RETENTION_DAYS=14,
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "testdb",
                        "USER": "u",
                        "PASSWORD": "p",
                        "HOST": "localhost",
                        "PORT": "5432",
                    }
                },
            ):
                with patch("business.backup._run_pg_dump", side_effect=fake_pg_dump):
                    result = create_backup(prune=False)

            self.assertEqual(result.status, "ok")
            dest = Path(result.directory)
            self.assertTrue((dest / "db.dump").is_file())
            self.assertTrue((dest / "media.tar.gz").is_file())
            self.assertTrue((dest / "manifest.json").is_file())
            man = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(man["status"], "ok")
            with tarfile.open(dest / "media.tar.gz", "r:gz") as tar:
                names = tar.getnames()
            self.assertTrue(any(n.endswith("logo.txt") for n in names))

            items = list_backups(backup_dir)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["stamp"], result.stamp)

    @override_settings(BACKUP_ENABLED=False)
    def test_disabled_raises(self):
        with self.assertRaises(BackupError):
            create_backup()


class RestoreHelperTests(SimpleTestCase):
    def test_adapt_json_dict_and_list(self):
        from psycopg.types.json import Jsonb

        from business.restore import _adapt_row

        row = _adapt_row(
            (1, {"a": 1}, [{"from": 30}], "ok"),
            ["int4", "jsonb", "jsonb", "text"],
        )
        self.assertEqual(row[0], 1)
        self.assertIsInstance(row[1], Jsonb)
        self.assertIsInstance(row[2], Jsonb)
        self.assertEqual(row[3], "ok")

    def test_safe_stamp(self):
        from business.restore import is_safe_stamp

        self.assertTrue(is_safe_stamp("20260922_163000"))
        self.assertFalse(is_safe_stamp("../etc"))
        self.assertFalse(is_safe_stamp("exports"))
        self.assertFalse(is_safe_stamp("a/b"))

    def test_classify_pg_restore_warnings_are_not_hard_failure(self):
        from business.restore import classify_pg_restore_output

        hard, messages = classify_pg_restore_output(
            1, "pg_restore: warning: errors ignored on restore: 2"
        )
        self.assertFalse(hard)
        self.assertTrue(messages)

    def test_classify_pg_restore_error_is_hard_failure(self):
        from business.restore import classify_pg_restore_output

        hard, messages = classify_pg_restore_output(
            1, "pg_restore: error: could not execute query: ERROR:  permission denied"
        )
        self.assertTrue(hard)
        self.assertTrue(any("error" in m.lower() for m in messages))

    def test_format_restore_message_includes_reason(self):
        from business.restore import RestoreResult, format_restore_message

        result = RestoreResult(
            stamp="20260922_163000",
            mode="merge",
            status="failed",
            errors=["Table clients: duplicate key value violates unique constraint"],
        )
        text = format_restore_message(result)
        self.assertIn("20260922_163000", text)
        self.assertIn("duplicate key", text)

    def test_missing_backup_raises_with_reason(self):
        import tempfile

        from business.restore import restore_from_stamp

        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(BACKUP_DIR=tmp):
                with self.assertRaises(BackupError) as ctx:
                    restore_from_stamp("20260101_000000", merge=False)
                self.assertIn("Backup not found", str(ctx.exception))

    def test_classify_downloaded_backup_names(self):
        from business.restore import classify_backup_filename

        self.assertEqual(classify_backup_filename("sales_db_20260922_163000.dump"), "db")
        self.assertEqual(
            classify_backup_filename("sales_media_20260922_163000.tar.gz"), "media"
        )
        self.assertEqual(classify_backup_filename("backup.zip"), "zip")
        self.assertEqual(classify_backup_filename("notes.txt"), "unknown")

    def test_restore_from_unrecognised_upload_explains_reason(self):
        import tempfile

        from business.restore import restore_from_uploaded_files

        with tempfile.TemporaryDirectory() as tmp:
            junk = Path(tmp) / "readme.txt"
            junk.write_text("not a backup", encoding="utf-8")
            with override_settings(BACKUP_DIR=str(Path(tmp) / "backups")):
                with self.assertRaises(BackupError) as ctx:
                    restore_from_uploaded_files([junk], merge=False)
                self.assertIn("Could not recognise", str(ctx.exception))

    def test_extract_zip_finds_dump_and_media(self):
        import tempfile
        import zipfile

        from business.restore import _extract_backup_zip

        with tempfile.TemporaryDirectory() as tmp:
            zpath = Path(tmp) / "pack.zip"
            dest = Path(tmp) / "out"
            dest.mkdir()
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("folder/db.dump", b"PGDMP")
                zf.writestr("folder/media.tar.gz", b"media")
            has_db, has_media = _extract_backup_zip(zpath, dest)
            self.assertTrue(has_db)
            self.assertTrue(has_media)
            self.assertTrue((dest / "db.dump").is_file())
            self.assertTrue((dest / "media.tar.gz").is_file())


class BackupRestoreViewTests(TestCase):
    def setUp(self):
        from accounts.models import User

        self.user = User.objects.create_user(
            email="setup@example.com", password="pass12345"
        )
        self.client.login(email="setup@example.com", password="pass12345")

    def test_company_setup_shows_restore_without_profile(self):
        response = self.client.get(reverse("company_setup"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Restore from backup")

    def test_backup_list_allowed_without_company(self):
        response = self.client.get(reverse("backup_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Backups")

    def test_restore_page_allowed_without_company(self):
        response = self.client.get(reverse("restore_page"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Restore uploaded files")
        self.assertContains(response, "Database dump")

    def test_restore_upload_without_files_shows_reason(self):
        response = self.client.post(reverse("restore_page"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose the backup file")

    def test_restore_without_company_uses_full_mode(self):
        from business.restore import RestoreResult

        result = RestoreResult(
            stamp="20260922_163000", mode="full", status="ok", db=True, media=True
        )
        with patch(
            "business.restore.restore_from_stamp", return_value=result
        ) as mocked:
            response = self.client.post(
                reverse("backup_restore", args=["20260922_163000"]),
                follow=True,
            )
        mocked.assert_called_once()
        self.assertFalse(mocked.call_args.kwargs.get("merge"))
        self.assertEqual(response.status_code, 200)

    def test_restore_error_is_shown_with_reason(self):
        with patch(
            "business.restore.restore_from_stamp",
            side_effect=BackupError("pg_restore not found. Install PostgreSQL client tools."),
        ):
            response = self.client.post(
                reverse("backup_restore", args=["20260922_163000"]),
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "pg_restore not found")

    def test_team_member_cannot_open_backups(self):
        from accounts.models import CompanyMember, CompanyProfile, User

        owner = User.objects.create_user(
            email="owner@example.com", password="pass12345"
        )
        company = CompanyProfile.objects.create(user=owner, company_name="Acme")
        CompanyMember.objects.update_or_create(
            company=company,
            user=owner,
            defaults={
                "role": CompanyMember.Role.OWNER,
                "sales_access": True,
                "can_grant_sales": True,
                "support_ticket_access": True,
            },
        )
        member = User.objects.create_user(
            email="member@example.com", password="pass12345"
        )
        CompanyMember.objects.create(
            company=company,
            user=member,
            role=CompanyMember.Role.MEMBER,
            sales_access=True,
            can_grant_sales=False,
            support_ticket_access=True,
        )
        self.client.logout()
        self.client.login(email="member@example.com", password="pass12345")
        response = self.client.get(reverse("backup_list"))
        self.assertEqual(response.status_code, 302)

    def test_owner_restore_uses_merge_mode(self):
        from accounts.models import CompanyMember, CompanyProfile
        from business.restore import RestoreResult

        company = CompanyProfile.objects.create(
            user=self.user, company_name="Acme"
        )
        CompanyMember.objects.update_or_create(
            company=company,
            user=self.user,
            defaults={
                "role": CompanyMember.Role.OWNER,
                "sales_access": True,
                "can_grant_sales": True,
                "support_ticket_access": True,
            },
        )
        result = RestoreResult(
            stamp="20260922_163000", mode="merge", status="ok", db=True, media=True
        )
        with patch(
            "business.restore.restore_from_stamp", return_value=result
        ) as mocked:
            response = self.client.post(
                reverse("backup_restore", args=["20260922_163000"]),
                follow=True,
            )
        mocked.assert_called_once()
        self.assertTrue(mocked.call_args.kwargs.get("merge"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Restore completed")

