from pathlib import Path
import hashlib
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from core.flood_camera_monitoring.infra.models import FloodModelVersion
from core.flood_camera_monitoring.services.model_registry import load_provider


class Command(BaseCommand):
    help = 'Register a local artifact and manifest; optionally register the already deployed legacy model as initial champion.'

    def add_arguments(self, parser):
        parser.add_argument('artifact')
        parser.add_argument('manifest')
        parser.add_argument('--name', required=True)
        parser.add_argument('--provider', choices=['torch', 'onnx', 'http'], default='torch')
        parser.add_argument('--bootstrap-current', action='store_true')

    def handle(self, *args, **options):
        path = Path(options['artifact']).resolve()
        manifest = json.loads(Path(options['manifest']).read_text())
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        with transaction.atomic():
            if options['bootstrap_current']:
                from core.flood_camera_monitoring.infra.utils import resolve_checkpoint_path
                if path != resolve_checkpoint_path().resolve() or FloodModelVersion.objects.filter(status='champion').exists():
                    raise CommandError('Bootstrap is only allowed for the current configured checkpoint with no registered champion')
            version = FloodModelVersion(name=options['name'], artifact_path=str(path),
                manifest=manifest, provider=options['provider'], sha256=digest,
                status='champion' if options['bootstrap_current'] else 'shadow')
            load_provider(version)
            version.save()
        self.stdout.write(str(version.pk))
