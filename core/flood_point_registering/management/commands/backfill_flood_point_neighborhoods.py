import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.flood_point_registering.infra.models import Flood_Point_Register
from core.flood_point_registering.services import sync_flood_point_neighborhoods


class Command(BaseCommand):
    help = "Preenche os vinculos multibairro; exige exatamente --dry-run ou --apply."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--dry-run", action="store_true")
        group.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        total = Flood_Point_Register.objects.count()
        changed = 0
        with transaction.atomic():
            for point in Flood_Point_Register.objects.select_related("neighborhood").iterator():
                before = list(point.neighborhood_links.values_list("neighborhood_id", "is_primary"))
                sync_flood_point_neighborhoods(
                    point, method="BACKFILL_V1", reference_base_revision=None
                )
                after = list(point.neighborhood_links.values_list("neighborhood_id", "is_primary"))
                changed += before != after
            if options["dry_run"]:
                transaction.set_rollback(True)
        self.stdout.write(json.dumps({"mode": "dry-run" if options["dry_run"] else "apply", "examined": total, "changed": changed}, sort_keys=True))
