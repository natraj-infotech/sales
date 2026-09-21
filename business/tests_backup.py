"""Tests for combined backup helpers (no real pg_dump required)."""

import json
import tarfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
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
