"""List combined backups under BACKUP_DIR."""

import json

from django.core.management.base import BaseCommand

from business.backup import backup_root, list_backups


class Command(BaseCommand):
    help = "List available combined backups (newest first)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Print machine-readable JSON.",
        )

    def handle(self, *args, **options):
        items = list_backups()
        if options["json"]:
            self.stdout.write(json.dumps(items, indent=2))
            return

        root = backup_root()
        self.stdout.write(f"Backup root: {root}")
        if not items:
            self.stdout.write("No backups found.")
            return
        for item in items:
            man = item.get("manifest") or {}
            status = man.get("status", "?")
            created = man.get("created_at", "")
            self.stdout.write(
                f"- {item['stamp']}  status={status}  "
                f"db={'yes' if item['has_db'] else 'no'}  "
                f"media={'yes' if item['has_media'] else 'no'}  "
                f"{created}"
            )
