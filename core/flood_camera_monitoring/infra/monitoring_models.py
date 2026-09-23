import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class ImmutableRecord(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Audit records are immutable')
        return super().save(*args, **kwargs)


class CameraMonitoringState(models.Model):
    camera = models.OneToOneField('flood_camera_monitoring.Camera', on_delete=models.CASCADE, primary_key=True, related_name='monitoring')
    level = models.CharField(max_length=12, default='NORMAL')
    reason = models.CharField(max_length=200, default='INITIAL')
    next_analysis_at = models.DateTimeField(default=timezone.now, db_index=True)
    strong_streak = models.PositiveIntegerField(default=0)
    clear_streak = models.PositiveIntegerField(default=0)
    last_probability = models.FloatField(null=True)
    manual_until = models.DateTimeField(null=True)
    context_until = models.DateTimeField(null=True)
    lease_token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)


class CameraAnalysisRun(ImmutableRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    camera = models.ForeignKey('flood_camera_monitoring.Camera', on_delete=models.PROTECT, related_name='analysis_runs')
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(default=timezone.now, db_index=True)
    level = models.CharField(max_length=12)
    result = models.JSONField(default=dict)
    frames = models.JSONField(default=list)
    model_version = models.CharField(max_length=255, blank=True)
    context = models.JSONField(default=dict)
    latency_ms = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=80, blank=True)


class CameraEvidence(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(CameraAnalysisRun, on_delete=models.PROTECT, related_name='evidence')
    relative_path = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    captured_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    deleted_at = models.DateTimeField(null=True)
    size = models.PositiveIntegerField()


class CameraReview(ImmutableRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(CameraAnalysisRun, on_delete=models.PROTECT, related_name='reviews')
    actor = models.ForeignKey('users.User', on_delete=models.PROTECT)
    classification = models.CharField(max_length=32)
    reason = models.TextField()
    approved = models.BooleanField(default=False)
    event_id = models.CharField(max_length=100, blank=True)
    approval_of = models.ForeignKey('self', null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)


class MonitoringAudit(ImmutableRecord):
    camera = models.ForeignKey('flood_camera_monitoring.Camera', on_delete=models.PROTECT)
    actor = models.ForeignKey('users.User', null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)


class FloodModelVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    provider = models.CharField(max_length=32, default='torch')
    artifact_path = models.CharField(max_length=512)
    sha256 = models.CharField(max_length=64, unique=True)
    manifest = models.JSONField(default=dict)
    metrics = models.JSONField(default=dict)
    status = models.CharField(max_length=12, default='candidate')
    created_at = models.DateTimeField(default=timezone.now)
    approved_by = models.ForeignKey('users.User', null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['status'], condition=models.Q(status='champion'), name='one_flood_champion')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = type(self).objects.get(pk=self.pk)
            if any(getattr(old, field) != getattr(self, field) for field in ('provider', 'artifact_path', 'sha256', 'manifest')):
                raise ValidationError('Model artifact and manifest are immutable')
        return super().save(*args, **kwargs)


class ModelShadowResult(ImmutableRecord):
    run = models.ForeignKey(CameraAnalysisRun, on_delete=models.PROTECT, related_name='shadow_results')
    model = models.ForeignKey(FloodModelVersion, on_delete=models.PROTECT)
    probabilities = models.JSONField(default=list)
    error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'model'], name='unique_shadow_run_model')]
