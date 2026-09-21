"""Restore a combined backup (destructive for DB when --db is used)."""

from django.core.management.base import BaseCommand, CommandError

from business.backup import BackupError, list_backups, restore_backup


class Command(BaseCommand):
    help = (
        "Restore database and/or media from BACKUP_DIR/<stamp>/. "
        "Requires --confirm. Database restore uses pg_restore --clean."
    )

    def add_arguments(self, parser):
        parser.add_argument("stamp", nargs="?", help="Backup stamp folder name.")
        parser.add_argument(
            "--latest",
            action="store_true",
            help="Restore the newest backup that has a successful manifest.",
        )
        parser.add_argument(
            "--db",
            action="store_true",
            help="Restore PostgreSQL from db.dump.",
        )
        parser.add_argument(
            "--media",
            action="store_true",
            help="Restore MEDIA_ROOT from media.tar.gz.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required. Acknowledges this may overwrite data.",
        )

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError("Refusing to restore without --confirm.")

        stamp = options.get("stamp")
        if options["latest"]:
            for item in list_backups():
                man = item.get("manifest") or {}
                if man.get("status") == "ok" and item.get("has_db"):
                    stamp = item["stamp"]
                    break
            if not stamp:
                raise CommandError("No successful backup found to restore.")
        if not stamp:
            raise CommandError("Provide a stamp or use --latest.")

        restore_db = options["db"]
        restore_media = options["media"]
        if not restore_db and not restore_media:
            restore_db = True
            restore_media = True

        try:
            summary = restore_backup(
                stamp, restore_db=restore_db, restore_media=restore_media
            )
        except BackupError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"Restore finished: {summary}"))
