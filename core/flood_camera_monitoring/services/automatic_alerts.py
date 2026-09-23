from datetime import timedelta

from django.conf import settings
from core.users.infra.models import User
from django.utils import timezone

from core.flood_camera_monitoring.infra.models import CameraAnalysisRun, FloodModelVersion, OperationalAlert


def eligible_for_publication(runs):
    if len(runs) != 3:
        return False
    if len({run.model_version for run in runs}) != 1:
        return False
    # Distinct completed windows, not three frames in the same capture.
    chronological = sorted(runs, key=lambda run: run.started_at)
    if any(b.started_at <= a.finished_at for a, b in zip(chronological, chronological[1:])):
        return False
    for run in runs:
        if run.error_code or not run.frames or not run.context.get('neighbor_signal') or not run.context.get('quality_usable'):
            return False
        if sum(frame['flooded'] for frame in run.frames)/len(run.frames) < 90:
            return False
        if not run.evidence.filter(deleted_at__isnull=True, expires_at__gt=timezone.now()).exists():
            return False
    return True


def publish_if_eligible(camera_id):
    if not getattr(settings, 'FLOOD_AUTOPUBLISH_ENABLED', False):
        return False
    actor_id = getattr(settings, 'FLOOD_AUTOPUBLISH_ACTOR_ID', '')
    actor = User.objects.filter(pk=actor_id, is_active=True, type='admin').first() if actor_id else None
    champion = FloodModelVersion.objects.filter(status='champion').first()
    if not actor or not champion:
        return False
    from .model_registry import promotion_errors
    if promotion_errors(champion.metrics):
        return False
    runs = list(CameraAnalysisRun.objects.filter(camera_id=camera_id,
        started_at__gte=timezone.now()-timedelta(minutes=5)).order_by('-started_at')[:3])
    if not eligible_for_publication(runs) or runs[0].model_version != champion.sha256:
        return False
    alert = OperationalAlert.objects.filter(camera_id=camera_id, status='OPEN_INDICATION').first()
    if not alert:
        return False
    from .operational_alerts import confirm_operational_alert
    from core.notifications.services import schedule_publication_push
    confirm_operational_alert(alert.pk, actor, automatic=True,
        reason='Three non-overlapping strong windows with fresh contextual corroboration',
        on_published=schedule_publication_push)
    return True
