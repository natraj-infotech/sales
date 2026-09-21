"""Create a combined PostgreSQL + media backup."""

from django.core.management.base import BaseCommand, CommandError

from business.backup import BackupError, create_backup


class Command(BaseCommand):
    help = (
        "Create a combined backup (pg_dump + media.tar.gz + manifest) "
        "under BACKUP_DIR, then prune folders older than BACKUP_RETENTION_DAYS."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-prune",
            action="store_true",
            help="Do not delete old backups after creating this one.",
        )

    def handle(self, *args, **options):
        try:
            result = create_backup(prune=not options["no_prune"])
        except BackupError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"Backup OK: {result.directory}"))
        self.stdout.write(f"  db.dump     {result.db_bytes} bytes")
        self.stdout.write(f"  media.tar.gz {result.media_bytes} bytes")
        if result.pruned:
            self.stdout.write(f"  pruned: {', '.join(result.pruned)}")
