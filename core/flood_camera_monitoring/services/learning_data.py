"""Grouping persisted observations and immutable image-level feedback. No inference."""
import hashlib
import json
import math
import uuid
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone
from core.flood_camera_monitoring.infra.models import (
    Camera, CameraAnalysisRun, ReviewEpisode, EpisodeObservation, CuratedImage,
    EpisodeImage, ImageLabel, LearningDataset, MonitoringAudit,
)

CLASSES = ('NO_INDICATION', 'INTERMEDIATE_INDICATION', 'FLOOD_INDICATION')
LABELS = (*CLASSES, 'UNUSABLE', 'INDETERMINATE')


def lock(key=84710302):
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s)', [key])


def curated_root():
    return Path(settings.FLOOD_CURATED_ROOT).resolve()


def safe_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValidationError('Caminho de imagem inválido.')
    return path


def storage_usage():
    from django.db.models import Sum
    size = CuratedImage.objects.aggregate(size=Sum('size'))['size'] or 0
    model_root = Path(settings.FLOOD_MODEL_REGISTRY_ROOT)
    model_size = sum(p.stat().st_size for p in model_root.rglob('*') if p.is_file()) if model_root.exists() else 0
    return {'images': {'bytes': size, 'limit': settings.FLOOD_CURATED_LIMIT_BYTES, 'warning': size >= .8 * settings.FLOOD_CURATED_LIMIT_BYTES},
            'models': {'bytes': model_size, 'limit': settings.FLOOD_MODEL_LIMIT_BYTES, 'warning': model_size >= .8 * settings.FLOOD_MODEL_LIMIT_BYTES}}


def preserve(evidence):
    from .adaptive_monitoring import evidence_root
    existing = CuratedImage.objects.filter(pk=evidence.sha256).first()
    if existing:
        if not safe_path(curated_root(), existing.relative_path).is_file():
            raise ValidationError('Imagem selecionada indisponível.')
        return existing
    if evidence.deleted_at or evidence.expires_at <= timezone.now():
        raise ValidationError('Evidência operacional expirada.')
    content = safe_path(evidence_root(), evidence.relative_path).read_bytes()
    if hashlib.sha256(content).hexdigest() != evidence.sha256:
        raise ValidationError('Hash da evidência inválido.')
    # Called under the same PostgreSQL lock by all curated writers.
    if storage_usage()['images']['bytes'] + len(content) > settings.FLOOD_CURATED_LIMIT_BYTES:
        raise ValidationError('Limite de armazenamento de imagens atingido.')
    relative = f'{evidence.sha256[:2]}/{evidence.sha256}.jpg'
    path = safe_path(curated_root(), relative)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        with path.open('xb') as output:
            output.write(content)
        path.chmod(0o600)
    return CuratedImage.objects.create(sha256=evidence.sha256, relative_path=relative, size=len(content))


def classification(run):
    return run.result.get('classification', '')


def values(run):
    try:
        numbers = [float(frame['normal']) for frame in run.frames]
        valid = (not run.error_code and run.context.get('quality_usable', False) and bool(run.model_version)
                 and classification(run) in CLASSES and bool(numbers)
                 and all(math.isfinite(n) and 0 <= n <= 100 for n in numbers))
        return valid, sum(numbers) / len(numbers) if valid else None
    except (KeyError, TypeError, ValueError):
        return False, None


def select_images(episode):
    """Freeze a review round. Never substitute reviewed or displayed evidence."""
    existing = episode.images.filter(revision=episode.revision)
    if existing.filter(labels__isnull=False).exists():
        return
    # Unreviewed cards follow the current peak. Stale forms fail instead of
    # assigning their decision to replacement images. Labeled rounds freeze.
    existing.exclude(evidence__run_id__in=[episode.first_run_id, episode.peak_run_id]).delete()
    for run_id in dict.fromkeys([episode.first_run_id, episode.peak_run_id]):
        if existing.filter(evidence__run_id=run_id).exists() or existing.count() >= 2:
            continue
        run = CameraAnalysisRun.objects.get(pk=run_id)
        evidence = run.evidence.filter(deleted_at__isnull=True, expires_at__gt=timezone.now()).order_by('captured_at', 'relative_path').first()
        if evidence is None:
            continue
        image = preserve(evidence)
        # Predictions refer to exactly this selected frame, not an episode label.
        all_ids = list(run.evidence.order_by('captured_at', 'relative_path').values_list('id', flat=True))
        index = all_ids.index(evidence.id)
        prediction = run.frames[index] if index < len(run.frames) else {}
        EpisodeImage.objects.create(episode=episode, revision=episode.revision, evidence=evidence,
            image=image, prediction=prediction, classification=classification(run))


@transaction.atomic
def ingest_run(run_id):
    lock()
    run = CameraAnalysisRun.objects.select_related('camera').get(pk=run_id)
    if EpisodeObservation.objects.filter(run=run).exists():
        return
    valid, normal = values(run)
    episode = ReviewEpisode.objects.filter(camera=run.camera, kind='divergence', closed_at__isnull=True).first()
    if episode and run.started_at <= episode.last_at:
        EpisodeObservation.objects.create(run=run, episode=None, valid=valid, normal=normal)
        return
    if episode and episode.model_version != run.model_version:
        episode.closed_at = run.started_at
        episode.save()
        episode = None
    if episode and (not valid or run.started_at - episode.last_at > timedelta(minutes=10)):
        episode.below_since = None
        episode.clear_streak = episode.changed_streak = 0
        episode.observed_seconds = 0
    threshold = settings.FLOOD_REVIEW_NORMAL_THRESHOLD
    strong = valid and classification(run) == 'FLOOD_INDICATION'
    if valid and (normal < threshold or strong) and episode is None:
        episode = ReviewEpisode.objects.create(camera=run.camera, model_version=run.model_version,
            started_at=run.started_at, last_at=run.started_at, first_run=run, peak_run=run,
            below_since=run.started_at if normal < threshold else None, minimum_normal=normal)
    if episode:
        if valid:
            episode.latest_normal = normal
            if normal < threshold:
                episode.below_since = episode.below_since or run.started_at
                episode.observed_seconds = int((run.started_at - episode.below_since).total_seconds())
            else:
                episode.below_since = None
            episode.clear_streak = episode.clear_streak + 1 if normal >= threshold and not strong else 0
            if normal < episode.minimum_normal:
                episode.minimum_normal, episode.peak_run = normal, run
            if strong:
                episode.visible, episode.reason = True, 'STRONG'
            elif episode.observed_seconds >= settings.FLOOD_REVIEW_PERSIST_SECONDS:
                episode.visible, episode.reason = True, 'PERSISTENT'
            current = classification(run)
            if episode.classification and current != episode.classification:
                episode.changed_streak = episode.changed_streak + 1 if episode.changed_class == current else 1
                episode.changed_class = current
                if episode.changed_streak >= 2:
                    episode.revision += 1
                    episode.first_run = episode.peak_run = run
                    episode.minimum_normal = normal
                    episode.classification = current
                    episode.changed_streak = 0
            else:
                episode.changed_streak = 0
            episode.classification = episode.classification or current
            if episode.clear_streak >= 2:
                episode.closed_at = run.started_at
        episode.last_at = run.started_at
        episode.save()
        if episode.visible:
            select_images(episode)
    elif valid and normal >= threshold:
        local = run.started_at.astimezone(ZoneInfo(settings.FLOOD_REVIEW_TIMEZONE))
        night = local.hour < 6 or local.hour >= 18
        if night == bool(local.date().toordinal() % 2):
            episode, created = ReviewEpisode.objects.get_or_create(camera=run.camera, kind='audit', audit_date=local.date(),
                defaults=dict(model_version=run.model_version, started_at=run.started_at, last_at=run.started_at,
                    closed_at=run.started_at, first_run=run, peak_run=run, visible=True, reason='NORMAL_AUDIT',
                    classification=classification(run), minimum_normal=normal, latest_normal=normal))
            if created:
                select_images(episode)
    EpisodeObservation.objects.create(run=run, episode=episode, valid=valid, normal=normal)


@transaction.atomic
def label_image(selection_id, actor, label, reason, request_id, previous_id=None):
    lock()
    if label not in LABELS or not reason.strip():
        raise ValidationError('Informe classificação e justificativa.')
    selection = EpisodeImage.objects.select_related('episode', 'evidence').get(pk=selection_id)
    existing = ImageLabel.objects.filter(request_id=request_id).first()
    if existing:
        if existing.selection_id != selection.id or existing.actor_id != actor.id or existing.classification != label or existing.reason != reason:
            raise ValidationError('Chave de idempotência já utilizada em outra decisão.')
        return existing
    previous = selection.labels.order_by('-created_at', '-id').first()
    if str(previous.pk if previous else '') != str(previous_id or ''):
        raise ValidationError('Esta imagem recebeu outra revisão. Atualize antes de corrigir.')
    return ImageLabel.objects.create(selection=selection, actor=actor, classification=label,
        previous_classification=previous.classification if previous else selection.classification,
        reason=reason.strip(), request_id=request_id)


@transaction.atomic
def link_event(episode_id, event_id, actor):
    lock()
    episode = ReviewEpisode.objects.get(pk=episode_id)
    previous = str(episode.event_id)
    episode.event_id = uuid.UUID(str(event_id))
    episode.save(update_fields=['event_id'])
    MonitoringAudit.objects.create(camera=episode.camera, actor=actor, action='REVIEW_EVENT_LINK',
        details={'episode': str(episode.pk), 'previous': previous, 'event': str(episode.event_id)})


def current_rows():
    latest = {}
    for label in ImageLabel.objects.select_related('selection__episode__camera', 'selection__evidence__run', 'selection__image').order_by('-created_at', '-id'):
        # A byte-identical image has one latest human decision, globally.
        latest.setdefault(label.selection.image_id, label)
    # Identical bytes connect event groups even if observed by different cameras.
    parents = {}
    def root(value):
        parents.setdefault(value, value)
        while parents[value] != value:
            value = parents[value]
        return value
    by_hash = {}
    for digest, event in EpisodeImage.objects.filter(labels__isnull=False).values_list('image_id', 'episode__event_id').distinct():
        event = str(event)
        other = by_hash.setdefault(digest, event)
        a, b = root(event), root(other)
        parents[max(a,b)] = min(a,b)
    rows = []
    for digest, label in latest.items():
        if label.classification not in CLASSES:
            continue
        selection = label.selection
        episode = selection.episode
        event = root(str(episode.event_id))
        split_hash = int(hashlib.sha256(event.encode()).hexdigest(), 16) % 10
        split = 'test' if split_hash < 4 else 'validation' if split_hash == 4 else 'train'
        rows.append({'sha256': digest, 'label_id': str(label.pk), 'classification': label.classification,
            'event_id': event, 'episode_id': str(episode.pk), 'split': split,
            'camera_id': str(episode.camera_id), 'camera_name': episode.camera.description,
            'captured_at': selection.evidence.captured_at.isoformat(), 'path': selection.image.relative_path})
    return sorted(rows, key=lambda row: row['sha256'])


def readiness(rows):
    counts = {part: {label: sum(r['split'] == part and r['classification'] == label for r in rows) for label in CLASSES} for part in ('train', 'validation', 'test')}
    errors = []
    if len(rows) < 200: errors.append(f'Faltam {200-len(rows)} exemplos para o mínimo de 200.')
    groups = len({r['event_id'] for r in rows})
    if groups < 5: errors.append(f'Faltam {5-groups} grupos de eventos para o mínimo de cinco.')
    if sum(counts['test'].values()) < 100: errors.append('O teste precisa de pelo menos 100 imagens.')
    if counts['test']['FLOOD_INDICATION'] < 30: errors.append('O teste precisa de pelo menos 30 imagens de alagamento.')
    for part in ('train', 'validation', 'test'):
        if not all(counts[part].values()): errors.append(f'A partição {part} precisa conter as três classes.')
    groups_parts, hashes = {}, set()
    for row in rows:
        if row['sha256'] in hashes: errors.append('Há imagens duplicadas.')
        hashes.add(row['sha256'])
        old = groups_parts.setdefault(row['event_id'], row['split'])
        if old != row['split']: errors.append('Um evento aparece em mais de uma partição.')
    return {'ready': not errors, 'blockers': errors, 'count': len(rows), 'events': groups, 'partitions': counts}


def dataset_current(dataset, rows=None):
    latest = {row['sha256']: row for row in (current_rows() if rows is None else rows)}
    return all(latest.get(row['sha256']) == row for row in dataset.manifest['rows'])


@transaction.atomic
def create_dataset(actor):
    lock()
    rows = current_rows()
    if not rows:
        raise ValidationError('Valide pelo menos uma imagem antes de criar um conjunto.')
    manifest = {'schema': 1, 'rows': rows, 'readiness': readiness(rows), 'split_strategy': 'event_sha256_40_test_10_validation_50_train'}
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return LearningDataset.objects.get_or_create(sha256=digest, defaults={'actor': actor, 'manifest': manifest})[0]
