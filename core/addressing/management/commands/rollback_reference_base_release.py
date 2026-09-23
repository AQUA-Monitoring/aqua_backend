import json
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from core.addressing.reference_releases import promote_release


class Command(BaseCommand):
    help = "Reativa transacionalmente uma release superseded."
    def add_arguments(self, parser):
        parser.add_argument("revision", nargs="?")
        parser.add_argument("--to-revision")
        parser.add_argument("--expected-revision", required=True)
        parser.add_argument("--justification", required=True)
        parser.add_argument("--actor-id", required=True)
    def handle(self, *args, **options):
        revision = options["to_revision"] or options["revision"]
        if not revision: raise CommandError("Informe revision ou --to-revision.")
        try:
            actor = get_user_model().objects.get(pk=options["actor_id"])
        except (get_user_model().DoesNotExist, ValueError) as exc:
            raise CommandError("Administrador informado em --actor-id nao existe.") from exc
        release = promote_release(revision, expected_revision=options["expected_revision"], justification=options["justification"], actor=actor, rollback=True)
        self.stdout.write(json.dumps({"revision": release.revision, "status": release.status, "rollback": True}, sort_keys=True))
