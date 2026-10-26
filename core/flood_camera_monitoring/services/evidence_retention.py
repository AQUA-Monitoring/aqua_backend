"""Fifteen-day operational retention with a bounded archive of extreme frames."""
import math
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from core.flood_camera_monitoring.infra.models import CameraEvidence


def evidence_path(relative):
    root = Path(settings.FLOOD_PRIVATE_EVIDENCE_ROOT).resolve()
    curated = Path(getattr(settings, 'FLOOD_CURATED_ROOT', root / 'curated')).resolve()
    original = root / relative
    path = original.resolve()
    if original.is_symlink():
        raise ValueError('Evidence path is a symbolic link')
    if path == root or not path.is_relative_to(root) or path.is_relative_to(curated):
        raise ValueError('Evidence path outside operational storage')
    return path


def extreme_score(frames, relative):
    """Capture filenames encode the position in the model's frame predictions."""
    try:
        index = int(Path(relative).stem)
        if index < 0:
            return None
        score = frames[index]['flooded']
    except (ValueError, IndexError, KeyError, TypeError):
        return None
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return score if math.isfinite(score) and 0 <= score <= 100 else None


def extreme_ids(limit):
    if limit == 0:
        return []
    candidates = CameraEvidence.objects.filter(
        deleted_at__isnull=True, preserved_at__isnull=True,
        run__error_code='', run__context__quality_usable=True,
        run__result__classification='FLOOD_INDICATION',
    ).exclude(run__model_version='').values(
        'id', 'relative_path', 'sha256', 'captured_at', 'run__camera_id', 'run__frames')
    peaks = {}
    for item in candidates.iterator(chunk_size=500):
        score = extreme_score(item['run__frames'], item['relative_path'])
        if score is None:
            continue
        group = (item['run__camera_id'], timezone.localdate(item['captured_at']))
        rank = (score, item['captured_at'], str(item['id']))
        previous = peaks.get(group)
        if previous is None or rank > previous[0]:
            if evidence_path(item['relative_path']).is_file():
                peaks[group] = (rank, item['id'], item['sha256'])
    selected, hashes = [], set()
    for _, pk, digest in sorted(peaks.values(), reverse=True):
        if digest not in hashes:
            hashes.add(digest)
            selected.append(pk)
        if len(selected) == limit:
            break
    return selected


@transaction.atomic
def expire_evidence(batch_size=5000):
    limit = int(getattr(settings, 'FLOOD_EXTREME_EVIDENCE_LIMIT', 20))
    if not 0 <= limit <= 100 or not 1 <= batch_size <= 5000:
        raise ValueError('Invalid evidence retention limits')
    # Serialize archive replacement and expiry across overlapping Celery deliveries.
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_xact_lock(%s)', [73190422])
            if not cursor.fetchone()[0]:
                return 0
    now = timezone.now()
    selected = extreme_ids(limit)
    CameraEvidence.objects.filter(extreme_preserved=True).exclude(pk__in=selected).update(extreme_preserved=False)
    CameraEvidence.objects.filter(pk__in=selected, deleted_at__isnull=True,
        preserved_at__isnull=True).update(extreme_preserved=True)
    due = Q(expires_at__lte=now) | Q(captured_at__lte=now-timedelta(days=15))
    items = list(CameraEvidence.objects.select_for_update().filter(
        due, deleted_at__isnull=True, preserved_at__isnull=True, extreme_preserved=False,
    ).order_by('captured_at')[:batch_size])
    # Validate the whole batch before touching any file.
    paths = [evidence_path(item.relative_path) for item in items]
    for item, path in zip(items, paths):
        path.unlink(missing_ok=True)
        item.deleted_at = now
    if items:
        CameraEvidence.objects.bulk_update(items, ['deleted_at'])
    root = Path(settings.FLOOD_PRIVATE_EVIDENCE_ROOT).resolve()
    for parent in {path.parent for path in paths}:
        if parent != root:
            try:
                parent.rmdir()
            except OSError:
                pass
    return len(items)
