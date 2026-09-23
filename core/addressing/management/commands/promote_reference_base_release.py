import json
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from core.addressing.reference_releases import promote_release


class Command(BaseCommand):
    help = "Promove transacionalmente uma release validada."
    def add_arguments(self, parser):
        parser.add_argument("revision")
        parser.add_argument("--expected-revision", required=True)
        parser.add_argument("--justification", required=True)
        parser.add_argument("--actor-id", required=True)
    def handle(self, *args, **options):
        try:
            actor = get_user_model().objects.get(pk=options["actor_id"])
        except (get_user_model().DoesNotExist, ValueError) as exc:
            raise CommandError("Administrador informado em --actor-id nao existe.") from exc
        release = promote_release(options["revision"], expected_revision=options["expected_revision"], justification=options["justification"], actor=actor)
        self.stdout.write(json.dumps({"revision": release.revision, "status": release.status}, sort_keys=True))
