"""A single admitted, capacity-controlled shadow window, using existing evidence."""
import time
from django.db import connection
from django.utils import timezone
from core.flood_camera_monitoring.infra.models import ShadowWindow, ModelShadowResult
from .adaptive_monitoring import evidence_root
from .learning_data import safe_path
from .model_lifecycle import budget_ready, compatible


def evaluate_shadow(run_id):
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s)', [84710304])
        if not cursor.fetchone()[0]: return
    try:
        window = ShadowWindow.objects.select_related('session__model','run').filter(run_id=run_id,status='PENDING').first()
        if not window: return
        if not window.session.active or not budget_ready(start=True):
            ShadowWindow.objects.filter(pk=window.pk).update(status='EXPIRED'); return
        ShadowWindow.objects.filter(pk=window.pk).update(status='RUNNING')
        values, error = [], ''
        start = time.monotonic()
        try:
            provider = compatible(window.session.model)
            for evidence in window.run.evidence.filter(deleted_at__isnull=True,expires_at__gt=timezone.now()).order_by('captured_at'):
                if not budget_ready() or time.monotonic()-start > 20:
                    error = 'CAPACITY_PAUSED'; break
                result = provider.predict(safe_path(evidence_root(), evidence.relative_path).read_bytes())
                values.append({'normal':result.probabilities.normal,'medium':result.probabilities.medium,'flooded':result.probabilities.flooded})
            if not values: error = error or 'NO_EVIDENCE'
        except Exception as exc: error = type(exc).__name__
        ModelShadowResult.objects.get_or_create(run=window.run,model=window.session.model,
            defaults={'probabilities':values,'error_code':error})
        ShadowWindow.objects.filter(pk=window.pk).update(status='FAILED' if error else 'COMPLETED')
    finally:
        with connection.cursor() as cursor: cursor.execute('SELECT pg_advisory_unlock(%s)', [84710304])
