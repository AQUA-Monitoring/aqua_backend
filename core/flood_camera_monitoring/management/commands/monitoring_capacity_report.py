"""Read-only pilot report; never expands rollout or changes camera states."""
import json
import math
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.flood_camera_monitoring.infra.models import CameraAnalysisRun
from core.flood_camera_monitoring.services.monitoring_capacity import capacity_snapshot


class Command(BaseCommand):
    help = 'Report pilot dispatch latency, errors and observed coverage without running inference.'

    def add_arguments(self, parser):
        parser.add_argument('--camera-id', required=True)
        parser.add_argument('--hours', type=int, default=24)
        parser.add_argument('--policy-version', default='conservative:v1')

    def handle(self, *args, **options):
        if options['hours'] <= 0:
            raise CommandError('--hours must be positive')
        now = timezone.now()
        rows = list(CameraAnalysisRun.objects.filter(camera_id=options['camera_id'],
            started_at__gte=now-timedelta(hours=options['hours']),
            context__policy_version=options['policy_version']).order_by('started_at').values(
                'started_at', 'finished_at', 'error_code', 'latency_ms', 'context'))
        delays = sorted(row['context']['dispatch_delay_seconds'] for row in rows
                        if isinstance(row['context'].get('dispatch_delay_seconds'), (int, float)))
        overlaps = sum(b['started_at'] < a['finished_at'] for a, b in zip(rows, rows[1:]))
        first = CameraAnalysisRun.objects.filter(camera_id=options['camera_id'],
            context__policy_version=options['policy_version']).order_by('started_at').values_list('started_at', flat=True).first()
        coverage = (now-first).total_seconds()/3600 if first else 0
        within = sum(delay <= 20 for delay in delays)/len(delays) if delays else None
        payload = {'camera_id': options['camera_id'], 'policy_version': options['policy_version'],
            'hours_since_first_run': round(coverage, 2), 'runs_in_window': len(rows),
            'errors': sum(bool(row['error_code']) for row in rows), 'overlapping_runs': overlaps,
            'dispatch_p95_seconds': delays[math.ceil(.95*len(delays))-1] if delays else None,
            'dispatch_within_20s_fraction': within,
            'observation_period_complete': coverage >= options['hours'],
            'latency_target_met': within is not None and within >= .95,
            'capacity_now': capacity_snapshot(),
            'expansion': 'MANUAL_REVIEW_REQUIRED',
            'note': 'Also inspect queue-delay trends and host pressure throughout the pilot; current capacity is not historical proof.'}
        self.stdout.write(json.dumps(payload, default=str))
