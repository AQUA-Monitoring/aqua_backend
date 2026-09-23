"""Replay captured images without modifying camera state or sending alerts."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import resource
import time

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Replay a preserved incident and measure local inference throughput (no database writes).'

    def add_arguments(self, parser):
        parser.add_argument('directory')
        parser.add_argument('--repetitions', type=int, default=1)
        parser.add_argument('--concurrency', type=int, default=1)

    def handle(self, *args, **options):
        from core.flood_camera_monitoring.infra.torch_classifier import TorchFloodClassifier
        from core.flood_camera_monitoring.infra.utils import resolve_checkpoint_path
        root = Path(options['directory']).resolve()
        manifest = json.loads((root/'manifest.json').read_text())
        classifier = TorchFloodClassifier(resolve_checkpoint_path())
        if classifier._fallback:
            raise RuntimeError('Model fallback cannot benchmark operational inference')
        entries = []
        for capture in manifest['captures']:
            for item in capture['files']:
                if item['name'].endswith('.jpg'):
                    path = (root/capture['camera_id']/item['name']).resolve()
                    if not path.is_relative_to(root):
                        raise ValueError('Invalid incident path')
                    entries.append((capture['description'], path))
        def predict(entry):
            name, path = entry
            started = time.monotonic()
            result = classifier.predict(path)
            return {'camera': name, 'image': path.name, 'flooded': result.probabilities.flooded,
                'medium': result.probabilities.medium, 'normal': result.probabilities.normal,
                'latency_ms': round((time.monotonic()-started)*1000, 2)}
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=max(1, min(options['concurrency'], 4))) as executor:
            results = list(executor.map(predict, entries*max(1, min(options['repetitions'], 20))))
        elapsed = time.monotonic()-started
        latencies = sorted(result['latency_ms'] for result in results)
        self.stdout.write(json.dumps({'images': len(results), 'seconds': elapsed,
            'images_per_second': len(results)/max(elapsed, 1e-9),
            'p95_ms': latencies[min(len(latencies)-1, int(len(latencies)*.95))] if latencies else None,
            'max_rss_kb': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'results': results}, ensure_ascii=False))
