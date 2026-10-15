"""Per-camera scheduling, durable private evidence and auditable monitoring.

Database leases prevent duplicate execution. A task's hard timeout must remain
shorter than its lease. Evidence is written before model inference.
"""
from datetime import timedelta
import hashlib
import errno
import shutil
from pathlib import Path
import uuid

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q, Case, When, Value, IntegerField
from django.utils import timezone

from core.flood_camera_monitoring.infra.models import (
    Camera, CameraMonitoringState, CameraAnalysisRun, CameraEvidence, MonitoringAudit,
)
from .adaptive_policy import (
    decide, decide_conservative, intervals_for, policy_for, mean_probability, trend_window,
)
from .monitoring_capacity import enabled as capacity_enabled, sample_capacity


def evidence_root():
    return Path(getattr(settings, 'FLOOD_PRIVATE_EVIDENCE_ROOT', '/app/private_flood_evidence')).resolve()


def ensure_evidence_capacity(root, incoming_bytes):
    reserve = int(getattr(settings, 'FLOOD_EVIDENCE_MIN_FREE_BYTES', 2 * 1024 ** 3))
    if shutil.disk_usage(root).free < reserve + incoming_bytes:
        raise OSError(errno.ENOSPC, 'Evidence storage reserve reached')


def queue_for(level):
    return 'flood_critical' if level in ('WATCH', 'CRITICAL') else 'flood_regular'


def reserve_due(limit=12):
    now = timezone.now()
    reserved = []
    with transaction.atomic():
        # Serialize capacity accounting across beat, legacy dispatch aliases and retries.
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_xact_lock(%s)', [73190421])
                if not cursor.fetchone()[0]:
                    return []
        elif capacity_enabled():
            raise RuntimeError('Capacity admission requires PostgreSQL')
        for camera_id in Camera.objects.filter(status=1).values_list('id', flat=True):
            CameraMonitoringState.objects.get_or_create(camera_id=camera_id)
        now = timezone.now()
        capacity = sample_capacity()
        remaining = {queue: capacity['limit_per_queue'] for queue in ('flood_regular', 'flood_critical')}
        if capacity_enabled():
            for active in CameraMonitoringState.objects.filter(lease_until__gt=now).values('lease_queue', 'level'):
                queue = active['lease_queue'] or queue_for(active['level'])
                remaining[queue] -= 1
        states = CameraMonitoringState.objects.select_for_update(skip_locked=True).filter(
            camera__status=1, next_analysis_at__lte=now).filter(
            Q(lease_until__isnull=True) | Q(lease_until__lte=now)).annotate(
            priority=Case(When(level='CRITICAL', then=Value(0)),
                          When(level='WATCH', then=Value(1)), default=Value(2), output_field=IntegerField())
            ).order_by('priority', 'next_analysis_at', 'camera_id')
        for state in states:
            queue = queue_for(state.level)
            if len(reserved) >= limit or (capacity_enabled() and remaining[queue] <= 0):
                if state.capacity_deferred_at is None:
                    state.capacity_deferred_at = now
                    state.save(update_fields=['capacity_deferred_at'])
                continue
            state.lease_token = uuid.uuid4()
            # Delivery expires in 120s; after claiming the execution gets a fresh lease.
            state.lease_until = now + timedelta(seconds=180)
            state.lease_reserved_at = now
            state.lease_started_at = None
            state.lease_queue = queue
            state.capacity_deferred_at = None
            state.save(update_fields=['lease_token', 'lease_until', 'lease_reserved_at',
                                      'lease_started_at', 'lease_queue', 'capacity_deferred_at'])
            remaining[queue] -= 1
            reserved.append((str(state.camera_id), str(state.lease_token), state.level))
    return reserved


def release_reservation(camera_id, token):
    # Only release a task that has not claimed its lease (e.g. broker publish failed).
    CameraMonitoringState.objects.filter(camera_id=camera_id, lease_token=token,
        lease_started_at__isnull=True).update(lease_token=None, lease_until=None,
                                            lease_queue='', lease_reserved_at=None)


def analyze_reserved(camera_id, token):
    from .analyze import AnalyzeAllCamerasService
    started = timezone.now()
    with transaction.atomic():
        state = CameraMonitoringState.objects.select_for_update().get(camera_id=camera_id)
        if (str(state.lease_token) != str(token) or state.lease_started_at is not None
                or not state.lease_until or state.lease_until <= started):
            return
        # Claim token exactly once; duplicate task deliveries cannot execute.
        state.lease_started_at = started
        state.lease_until = started + timedelta(seconds=180)
        state.save(update_fields=['lease_started_at', 'lease_until'])
    camera = Camera.objects.get(pk=camera_id)
    if camera.status != 1:
        CameraMonitoringState.objects.filter(pk=camera_id, lease_token=token).update(
            lease_until=None, lease_token=None, lease_queue='', lease_started_at=None)
        return
    policy = policy_for(camera_id)
    run_id = uuid.uuid4()
    stored = []
    predictions = []
    quality = []
    errors = []
    def preserve(_camera, frames):
        root = evidence_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        ensure_evidence_capacity(root, sum(len(frame) for frame in frames))
        folder = root / str(run_id)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        for index, frame in enumerate(frames):
            import cv2
            import numpy as np
            decoded = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            quality.append({'decodable': decoded is not None,
                'brightness': float(decoded.mean()) if decoded is not None else None,
                'sharpness': float(cv2.Laplacian(decoded, cv2.CV_64F).var()) if decoded is not None else None})
            path = folder / f'{index:03d}.jpg'
            with path.open('xb') as output:
                output.write(frame)
            path.chmod(0o600)
            stored.append({'relative_path': str(path.relative_to(evidence_root())),
                'sha256': hashlib.sha256(frame).hexdigest(), 'size': len(frame),
                'captured_at': timezone.now()})
    def observe(_camera, summary, assessments):
        for assessment in assessments:
            predictions.append({'normal': assessment.probabilities.normal,
                'medium': assessment.probabilities.medium, 'flooded': assessment.probabilities.flooded})
    service = AnalyzeAllCamerasService(camera_id=camera_id, capture_observer=preserve,
        prediction_observer=observe, sample_frames=3 if state.level == 'NORMAL' else 7,
        sample_interval_ms=150 if state.level == 'NORMAL' else 3000, drain_between_samples=True,
        persist_legacy_images=False)
    result = {}
    try:
        payload, _ = service.run_and_collect()
        result = payload[0] if payload else {}
    except Exception as exc:
        # Never expose source URLs or exception text through the administrative API.
        errors.append(type(exc).__name__)
        from .operational_snapshot import get_or_create_operational_snapshot, mark_error
        mark_error(get_or_create_operational_snapshot(camera), error_code='MONITORING_FAILED')
    snapshot = getattr(Camera.objects.get(pk=camera_id), 'operational_snapshot', None)
    classification = getattr(snapshot, 'classification', None) if not errors else None
    probability = getattr(snapshot, 'prob_flooded', None)
    now = timezone.now()
    neighbors = Camera.objects.filter(status=1, region_id=camera.region_id,
        operational_snapshot__classification='FLOOD_INDICATION',
        operational_snapshot__analysis_status='AVAILABLE',
        operational_snapshot__analyzed_at__gte=now-timedelta(seconds=120)).exclude(pk=camera_id)
    contextual = bool(camera.region_id and neighbors.exists()) or bool(state.context_until and state.context_until > now)
    error_code = ','.join(errors) or getattr(snapshot, 'error_code', '') or ''
    model_version = getattr(snapshot, 'model_version', '') or ''
    quality_usable = bool(quality) and all(q['decodable'] and 15 < q['brightness'] < 245
                                          and q['sharpness'] > 5 for q in quality)
    mean = mean_probability(predictions)
    valid = (not error_code and quality_usable and mean is not None and bool(model_version)
             and getattr(snapshot, 'analysis_status', None) == 'AVAILABLE'
             and getattr(snapshot, 'model_status', None) == 'READY'
             and classification in {'NO_INDICATION', 'INTERMEDIATE_INDICATION', 'FLOOD_INDICATION'})
    current = {'valid': valid, 'model_version': model_version, 'policy': policy,
               'probability': mean, 'started_at': started}
    previous = list(CameraAnalysisRun.objects.filter(camera=camera).order_by('-started_at')[:2])
    observations = [{'valid': r.context.get('policy_valid', False), 'model_version': r.model_version,
                     'policy': r.context.get('policy_version'), 'probability': r.context.get('mean_flooded'),
                     'started_at': r.started_at} for r in reversed(previous)]
    trend = trend_window(observations, current, policy=policy)
    rising = trend['rising']
    with transaction.atomic():
        locked = CameraMonitoringState.objects.select_for_update().get(pk=camera_id)
        # Fence a late worker after its lease has expired or been replaced.
        if str(locked.lease_token) != str(token) or not locked.lease_until or locked.lease_until <= now:
            return
        continuous = (previous and previous[0].context.get('policy_version') == policy
                      and previous[0].context.get('policy_valid') and valid
                      and previous[0].model_version == model_version
                      and 0 < (started - previous[0].started_at).total_seconds()
                      <= getattr(settings, 'FLOOD_MONITORING_MAX_GAP_SECONDS', 600))
        manual = bool(locked.manual_until and locked.manual_until > now)
        expired_manual = bool(locked.manual_until and locked.manual_until <= now)
        if locked.policy_version != policy or expired_manual or (policy != 'legacy' and not continuous):
            locked.strong_streak = locked.clear_streak = 0
        if expired_manual:
            locked.manual_until = None
        if policy.startswith('conservative:'):
            decision = decide_conservative(locked.level, locked.strong_streak, locked.clear_streak,
                                           classification if valid else None, rising=rising)
        else:
            legacy_rising = probability is not None and locked.last_probability is not None and probability - locked.last_probability >= 10
            decision = decide(locked.level, locked.strong_streak, locked.clear_streak,
                              classification, contextual=contextual, rising=legacy_rising)
        old_level = locked.level
        if not manual:
            locked.level, locked.reason = decision.level, decision.reason
            locked.strong_streak, locked.clear_streak = decision.strong_streak, decision.clear_streak
        else:
            locked.strong_streak = locked.clear_streak = 0
        locked.policy_version = policy
        locked.last_probability = probability
        locked.next_analysis_at = now + timedelta(seconds=intervals_for(policy)[locked.level])
        locked.lease_until = locked.lease_token = None
        locked.lease_queue = ''
        locked.lease_started_at = locked.lease_reserved_at = None
        locked.save()
        run = CameraAnalysisRun.objects.create(id=run_id, camera=camera, started_at=started,
            level=state.level, result=result, frames=predictions, model_version=model_version,
            context={'neighbor_signal': contextual, 'rising': rising, 'quality': quality,
                'quality_usable': quality_usable,
                'duplicate_frames': len(stored)-len({item['sha256'] for item in stored}),
                'policy_version': policy, 'policy_valid': valid, 'mean_flooded': mean,
                'trend': trend, 'decision_reason': locked.reason, 'next_level': locked.level,
                'manual_active': manual,
                'dispatch_delay_seconds': max(0, ((state.lease_reserved_at or started) - state.next_analysis_at).total_seconds()),
                'queue_delay_seconds': max(0, (started - (state.lease_reserved_at or started)).total_seconds())},
            latency_ms=int((now-started).total_seconds()*1000), error_code=error_code)
        for values in stored:
            CameraEvidence.objects.create(run=run, expires_at=now+timedelta(days=90), **values)
        if old_level != locked.level:
            MonitoringAudit.objects.create(camera=camera, action='INTENSITY_CHANGED',
                details={'from': old_level, 'to': locked.level, 'reason': locked.reason,
                         'policy_version': policy, 'trend': trend})
        if policy == 'legacy' and locked.level == 'CRITICAL' and camera.region_id:
            # A legacy camera must not escalate a camera enrolled in the pilot.
            neighbors = CameraMonitoringState.objects.filter(camera__region_id=camera.region_id,
                camera__status=1, level='NORMAL').exclude(pk=camera_id)
            ids = [item.pk for item in neighbors if policy_for(item.pk) == 'legacy'
                   and (not item.manual_until or item.manual_until <= now)]
            CameraMonitoringState.objects.filter(pk__in=ids).update(
                level='WATCH', reason='REGIONAL_SIGNAL', next_analysis_at=now)
    from .automatic_alerts import publish_if_eligible
    publish_if_eligible(camera_id)
    return str(run.id)


def expire_evidence(batch_size=500):
    now = timezone.now()
    count = 0
    for item in CameraEvidence.objects.filter(expires_at__lte=now, deleted_at__isnull=True)[:batch_size]:
        path = (evidence_root() / item.relative_path).resolve()
        if not path.is_relative_to(evidence_root()):
            raise ValueError('Evidence path outside private storage')
        path.unlink(missing_ok=True)
        CameraEvidence.objects.filter(pk=item.pk, deleted_at__isnull=True).update(deleted_at=now)
        count += 1
    return count
