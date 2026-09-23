import json
from django.core.management.base import BaseCommand
from core.addressing.reference_releases import read_archive


class Command(BaseCommand):
    help = "Valida e inspeciona um pacote sem escrever no banco."
    def add_arguments(self, parser): parser.add_argument("archive")
    def handle(self, *args, **options):
        manifest, _payload, checksum = read_archive(options["archive"], load_payload=False)
        self.stdout.write(json.dumps({"valid": True, "archive_sha256": checksum, "manifest": manifest}, sort_keys=True))
