import json

from django.core.management.base import BaseCommand, CommandError

from core.addressing.models import ReferenceBaseRelease
from core.addressing.reference_releases import active_revision, build_payload, write_archive


class Command(BaseCommand):
    help = "Exporta uma release de referencia em .tar.gz deterministico."

    def add_arguments(self, parser):
        parser.add_argument("output")
        parser.add_argument("--revision")
        parser.add_argument("--release-id")

    def handle(self, *args, **options):
        release = None
        if options["release_id"]:
            try:
                release = ReferenceBaseRelease.objects.get(pk=options["release_id"])
            except (ReferenceBaseRelease.DoesNotExist, ValueError) as exc:
                raise CommandError("Release nao encontrada.") from exc
        revision = options["revision"] or (release.revision if release else None)
        if not revision:
            raise CommandError("--revision e obrigatoria ao exportar o catalogo atual.")
        expected_previous = release.manifest.get("expected_previous_revision") if release else active_revision()
        manifest, checksum = write_archive(
            options["output"], revision=revision, payload=build_payload(release),
            expected_previous_revision=expected_previous,
        )
        self.stdout.write(json.dumps({"revision": revision, "archive_sha256": checksum, "counts": {k: v["count"] for k, v in manifest["partitions"].items()}}, sort_keys=True))
