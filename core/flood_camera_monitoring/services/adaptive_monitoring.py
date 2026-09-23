"""Per-camera scheduling, durable private evidence and auditable monitoring.

Database leases prevent duplicate execution. A task's hard timeout must remain
shorter than its lease. Evidence is written before model inference.
"""
from datetime import timedelta
import hashlib
from pathlib import Path
import time
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Q, Case, When, Value, IntegerField
from django.utils import timezone

from core.flood_camera_monitoring.infra.models import (
    Camera, CameraMonitoringState, CameraAnalysisRun, CameraEvidence, MonitoringAudit,
)
from .adaptive_policy import INTERVALS, decide


def evidence_root():
    return Path(getattr(settings, 'FLOOD_PRIVATE_EVIDENCE_ROOT', '/app/private_flood_evidence')).resolve()


def reserve_due(limit=12):
    for camera_id in Camera.objects.filter(status=1).values_list('id', flat=True):
        CameraMonitoringState.objects.get_or_create(camera_id=camera_id)
    now = timezone.now()
    reserved = []
    with transaction.atomic():
        states = list(CameraMonitoringState.objects.select_for_update(skip_locked=True)
            .filter(camera__status=1, next_analysis_at__lte=now)
            .filter(Q(lease_until__isnull=True) | Q(lease_until__lt=now))
            .annotate(priority=Case(When(level='CRITICAL', then=Value(0)),
                When(level='WATCH', then=Value(1)), default=Value(2), output_field=IntegerField()))
            .order_by('priority', 'next_analysis_at')[:limit])
        states.sort(key=lambda state: INTERVALS[state.level])
        for state in states:
            state.lease_token = uuid.uuid4()
            state.lease_until = now + timedelta(seconds=180)
            state.save(update_fields=['lease_token', 'lease_until'])
            reserved.append((str(state.camera_id), str(state.lease_token), state.level))
    return reserved


def analyze_reserved(camera_id, token):
    from .analyze import AnalyzeAllCamerasService
    started = timezone.now()
    with transaction.atomic():
        state = CameraMonitoringState.objects.select_for_update().get(camera_id=camera_id)
        if str(state.lease_token) != str(token) or not state.lease_until or state.lease_until <= started:
            return
        # Claim token exactly once; duplicate task deliveries cannot execute.
        state.lease_token = None
        state.lease_until = started + timedelta(seconds=180)
        state.save(update_fields=['lease_token', 'lease_until'])
    camera = Camera.objects.get(pk=camera_id)
    if camera.status != 1:
        CameraMonitoringState.objects.filter(pk=camera_id).update(lease_until=None)
        return
    run_id = uuid.uuid4()
    stored = []
    predictions = []
    quality = []
    errors = []
    def preserve(_camera, frames):
        folder = evidence_root() / str(run_id)
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
    from core.flood_camera_monitoring.infra.models import FloodModelVersion
    from .model_registry import load_provider, verify_artifact
    from .model_artifact import ModelArtifactInfo
    champion = FloodModelVersion.objects.filter(status='champion').first()
    if champion:
        def inspect_champion():
            try:
                return ModelArtifactInfo(True, verify_artifact(champion), champion.sha256, None)
            except Exception:
                return ModelArtifactInfo(False, Path(champion.artifact_path), champion.sha256, 'MODEL_INVALID')
        service.artifact_inspector = inspect_champion
        service.classifier_factory = lambda _: load_provider(champion)
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
    rising = probability is not None and state.last_probability is not None and probability-state.last_probability >= 10
    with transaction.atomic():
        locked = CameraMonitoringState.objects.select_for_update().get(pk=camera_id)
        decision = decide(locked.level, locked.strong_streak, locked.clear_streak,
            classification, contextual=contextual, rising=rising)
        old_level = locked.level
        if not locked.manual_until or locked.manual_until <= now:
            locked.level = decision.level
            locked.reason = decision.reason
        locked.strong_streak, locked.clear_streak = decision.strong_streak, decision.clear_streak
        locked.last_probability = probability
        locked.next_analysis_at = now + timedelta(seconds=INTERVALS[locked.level])
        locked.lease_until = None
        locked.save()
        run = CameraAnalysisRun.objects.create(id=run_id, camera=camera, started_at=started,
            level=state.level, result=result, frames=predictions,
            model_version=getattr(snapshot, 'model_version', '') or '',
            context={'neighbor_signal': contextual, 'rising': rising, 'quality': quality,
                'quality_usable': bool(quality) and all(q['decodable'] and 15 < q['brightness'] < 245 and q['sharpness'] > 5 for q in quality),
                'duplicate_frames': len(stored)-len({item['sha256'] for item in stored})},
            latency_ms=int((now-started).total_seconds()*1000),
            error_code=','.join(errors) or getattr(snapshot, 'error_code', '') or '')
        for values in stored:
            CameraEvidence.objects.create(run=run, expires_at=now+timedelta(days=90), **values)
        if old_level != locked.level:
            MonitoringAudit.objects.create(camera=camera, action='INTENSITY_CHANGED',
                details={'from': old_level, 'to': locked.level, 'reason': locked.reason})
        if locked.level == 'CRITICAL' and camera.region_id:
            CameraMonitoringState.objects.filter(camera__region_id=camera.region_id,
                camera__status=1, level='NORMAL').exclude(pk=camera_id).update(
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
