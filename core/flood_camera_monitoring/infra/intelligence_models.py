"""Private, traceable human feedback and model lifecycle records."""
import uuid
from django.db import models
from django.utils import timezone
from .monitoring_models import ImmutableRecord


class ReviewEpisode(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    camera = models.ForeignKey('flood_camera_monitoring.Camera', on_delete=models.PROTECT)
    model_version = models.CharField(max_length=255)
    event_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    kind = models.CharField(max_length=16, default='divergence')
    reason = models.CharField(max_length=32, default='OBSERVING')
    started_at = models.DateTimeField()
    last_at = models.DateTimeField()
    below_since = models.DateTimeField(null=True)
    closed_at = models.DateTimeField(null=True)
    visible = models.BooleanField(default=False, db_index=True)
    clear_streak = models.PositiveIntegerField(default=0)
    classification = models.CharField(max_length=32, blank=True)
    changed_class = models.CharField(max_length=32, blank=True)
    changed_streak = models.PositiveIntegerField(default=0)
    revision = models.PositiveIntegerField(default=1)
    minimum_normal = models.FloatField(default=100)
    latest_normal = models.FloatField(default=100)
    observed_seconds = models.PositiveIntegerField(default=0)
    first_run = models.ForeignKey('flood_camera_monitoring.CameraAnalysisRun', on_delete=models.PROTECT, related_name='+')
    peak_run = models.ForeignKey('flood_camera_monitoring.CameraAnalysisRun', on_delete=models.PROTECT, related_name='+')
    audit_date = models.DateField(null=True)
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['camera'], condition=models.Q(closed_at__isnull=True, kind='divergence'), name='one_active_review_episode'),
            models.UniqueConstraint(fields=['camera', 'audit_date'], condition=models.Q(kind='audit'), name='one_normal_audit_daily'),
        ]
        indexes = [models.Index(fields=['visible', '-last_at'], name='review_episode_queue_idx')]


class EpisodeObservation(ImmutableRecord):
    run = models.OneToOneField('flood_camera_monitoring.CameraAnalysisRun', on_delete=models.PROTECT)
    episode = models.ForeignKey(ReviewEpisode, null=True, on_delete=models.PROTECT, related_name='observations')
    valid = models.BooleanField()
    normal = models.FloatField(null=True)


class CuratedImage(ImmutableRecord):
    sha256 = models.CharField(primary_key=True, max_length=64)
    relative_path = models.CharField(max_length=255)
    size = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now)


class EpisodeImage(ImmutableRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    episode = models.ForeignKey(ReviewEpisode, on_delete=models.PROTECT, related_name='images')
    revision = models.PositiveIntegerField()
    evidence = models.ForeignKey('flood_camera_monitoring.CameraEvidence', on_delete=models.PROTECT)
    image = models.ForeignKey(CuratedImage, on_delete=models.PROTECT)
    prediction = models.JSONField(default=dict)
    classification = models.CharField(max_length=32)
    created_at = models.DateTimeField(default=timezone.now)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['episode', 'revision', 'evidence'], name='unique_episode_image_revision')]


class ImageLabel(ImmutableRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    selection = models.ForeignKey(EpisodeImage, on_delete=models.PROTECT, related_name='labels')
    actor = models.ForeignKey('users.User', on_delete=models.PROTECT)
    classification = models.CharField(max_length=32)
    previous_classification = models.CharField(max_length=32)
    reason = models.TextField()
    request_id = models.UUIDField(unique=True)
    created_at = models.DateTimeField(default=timezone.now)


class LearningDataset(ImmutableRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sha256 = models.CharField(max_length=64, unique=True)
    manifest = models.JSONField(default=dict)
    actor = models.ForeignKey('users.User', on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)


class TrainingJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_id = models.UUIDField(unique=True)
    dataset = models.ForeignKey(LearningDataset, on_delete=models.PROTECT)
    parent = models.ForeignKey('flood_camera_monitoring.FloodModelVersion', on_delete=models.PROTECT, related_name='+')
    candidate = models.ForeignKey('flood_camera_monitoring.FloodModelVersion', null=True, on_delete=models.PROTECT, related_name='+')
    actor = models.ForeignKey('users.User', on_delete=models.PROTECT)
    status = models.CharField(max_length=24, default='WAITING_CAPACITY')
    requested_action = models.CharField(max_length=12, blank=True)
    progress = models.JSONField(default=dict)
    config = models.JSONField(default=dict)
    error_code = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['status'], condition=models.Q(status='TRAINING'), name='one_running_flood_training')]


class ModelEvaluation(ImmutableRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dataset = models.ForeignKey(LearningDataset, on_delete=models.PROTECT)
    model = models.ForeignKey('flood_camera_monitoring.FloodModelVersion', on_delete=models.PROTECT)
    metrics = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['dataset', 'model'], name='unique_model_dataset_evaluation')]


class ModelAssignment(models.Model):
    camera = models.OneToOneField('flood_camera_monitoring.Camera', primary_key=True, on_delete=models.PROTECT)
    model = models.ForeignKey('flood_camera_monitoring.FloodModelVersion', on_delete=models.PROTECT)
    started_at = models.DateTimeField(default=timezone.now)


class ModelDeployment(ImmutableRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_id = models.UUIDField(unique=True)
    actor = models.ForeignKey('users.User', on_delete=models.PROTECT)
    camera = models.ForeignKey('flood_camera_monitoring.Camera', null=True, on_delete=models.PROTECT)
    previous = models.ForeignKey('flood_camera_monitoring.FloodModelVersion', null=True, on_delete=models.PROTECT, related_name='+')
    target = models.ForeignKey('flood_camera_monitoring.FloodModelVersion', on_delete=models.PROTECT, related_name='+')
    action = models.CharField(max_length=24)
    reason = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)


class ShadowSession(models.Model):
    model = models.OneToOneField('flood_camera_monitoring.FloodModelVersion', on_delete=models.PROTECT)
    active = models.BooleanField(default=True)
    started_at = models.DateTimeField(default=timezone.now)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['active'], condition=models.Q(active=True), name='one_active_shadow_session')]


class ShadowWindow(models.Model):
    run = models.OneToOneField('flood_camera_monitoring.CameraAnalysisRun', on_delete=models.PROTECT)
    session = models.ForeignKey(ShadowSession, on_delete=models.PROTECT)
    status = models.CharField(max_length=12, default='PENDING')
    created_at = models.DateTimeField(default=timezone.now)
