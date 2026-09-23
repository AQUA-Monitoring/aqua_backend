import json
from django.core.management.base import BaseCommand
from core.addressing.reference_releases import import_release


class Command(BaseCommand):
    help = "Importa um pacote validado em staging; --dry-run nunca escreve."
    def add_arguments(self, parser):
        parser.add_argument("archive")
        parser.add_argument("--dry-run", action="store_true")
    def handle(self, *args, **options):
        release, manifest, checksum = import_release(options["archive"], dry_run=options["dry_run"])
        self.stdout.write(json.dumps({"dry_run": options["dry_run"], "revision": manifest["revision"], "release_id": str(release.id) if release else None, "status": release.status if release else "validated", "archive_sha256": checksum}, sort_keys=True))
