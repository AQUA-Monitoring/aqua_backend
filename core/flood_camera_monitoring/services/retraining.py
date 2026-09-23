"""Offline supervised candidate training, isolated from operational inference.

Only independently approved labels with explicit event IDs are eligible.
All frames from one event share one split. Training never promotes a model.
"""
from collections import defaultdict
import hashlib
from pathlib import Path
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from core.flood_camera_monitoring.infra.models import CameraReview, FloodModelVersion
from .adaptive_monitoring import evidence_root
from .model_registry import load_provider


def train_candidate(epochs=3):
    import torch
    from PIL import Image
    # Active critical regions pause training to reserve compute for response.
    from core.flood_camera_monitoring.infra.models import CameraMonitoringState
    if CameraMonitoringState.objects.filter(level='CRITICAL').exists():
        return {'status': 'paused_crisis'}
    champion = FloodModelVersion.objects.filter(status='champion', provider='torch').first()
    if champion is None:
        return {'status': 'missing_torch_champion'}
    rows = []
    seen_runs = set()
    reviews = CameraReview.objects.filter(approved=True).exclude(event_id='').select_related('run__camera').order_by('-created_at')
    for review in reviews:
        if review.run_id in seen_runs:
            continue
        seen_runs.add(review.run_id)
        if review.classification == 'UNUSABLE':
            continue
        evidence = review.run.evidence.filter(deleted_at__isnull=True, expires_at__gt=timezone.now()).first()
        if evidence:
            rows.append((review, evidence))
    events = sorted({review.event_id for review, _ in rows}, key=lambda value: hashlib.sha256(value.encode()).hexdigest())
    if len(rows) < 200 or len(events) < 5:
        return {'status': 'insufficient_labels', 'labels': len(rows), 'events': len(events)}
    if 'training_event_ids' not in champion.manifest:
        return {'status': 'parent_training_provenance_required'}
    # Stable assignment: adding events must not move old test events into train.
    test_events = {event for event in events if int(hashlib.sha256(event.encode()).hexdigest(), 16) % 5 == 0}
    if test_events & set(champion.manifest['training_event_ids']):
        raise ValidationError('Parent model has already trained on held-out events')
    train = [(r, e) for r, e in rows if r.event_id not in test_events]
    test = [(r, e) for r, e in rows if r.event_id in test_events]
    if not train or len(test) < 100 or sum(r.classification == 'FLOOD_INDICATION' for r, _ in test) < 30:
        return {'status': 'insufficient_held_out_labels'}
    if {e.sha256 for _, e in train} & {e.sha256 for _, e in test}:
        raise ValidationError('Identical images cross train/test boundary')
    dataset_hash = hashlib.sha256(''.join(sorted(str(r.pk)+e.sha256 for r, e in rows)).encode()).hexdigest()
    if FloodModelVersion.objects.filter(manifest__dataset_sha256=dataset_hash).exists():
        return {'status': 'dataset_already_trained'}
    provider = load_provider(champion)
    classes = provider.class_names
    labels = {'NO_INDICATION': 'normal', 'INTERMEDIATE_INDICATION': 'medium', 'FLOOD_INDICATION': 'flooded'}
    if any(labels[r.classification] not in classes for r, _ in rows):
        return {'status': 'classes_incompatible', 'classes': classes}
    torch.manual_seed(42)
    # Fine-tune the existing classifier head; preserve feature extraction.
    for parameter in provider.model.parameters():
        parameter.requires_grad = False
    head = getattr(getattr(provider.model, 'backbone', None), 'fc', None)
    if head is None:
        return {'status': 'unsupported_training_architecture'}
    for parameter in head.parameters():
        parameter.requires_grad = True
    optimizer = torch.optim.Adam(head.parameters(), lr=1e-4)
    def tensor(evidence):
        path = (evidence_root()/evidence.relative_path).resolve()
        if not path.is_relative_to(evidence_root()):
            raise ValidationError('Invalid evidence path')
        with Image.open(path) as image:
            return provider.transform(image.convert('RGB')).unsqueeze(0)
    provider.model.eval()
    for _ in range(max(1, min(int(epochs), 10))):
        for review, evidence in train:
            optimizer.zero_grad()
            logits = provider.model(tensor(evidence))
            loss = torch.nn.functional.cross_entropy(logits, torch.tensor([classes.index(labels[review.classification])]))
            loss.backward()
            optimizer.step()
    matrix = [[0 for _ in classes] for _ in classes]
    slices = defaultdict(lambda: {'tp': 0, 'fn': 0})
    brier = []
    terminal_passed = []
    flood_idx = classes.index('flooded')
    for review, evidence in test:
        with torch.inference_mode():
            probabilities = torch.softmax(provider.model(tensor(evidence)), dim=1)[0]
        actual = classes.index(labels[review.classification])
        predicted = int(probabilities.argmax())
        matrix[actual][predicted] += 1
        brier.append((float(probabilities[flood_idx])-int(actual == flood_idx))**2)
        if actual == flood_idx:
            for key in [str(review.run.camera_id), 'night' if review.run.started_at.hour >= 21 or review.run.started_at.hour < 9 else 'day']:
                slices[key]['tp' if predicted == flood_idx else 'fn'] += 1
            if 'terminal central' in review.run.camera.description.lower():
                terminal_passed.append(predicted == flood_idx)
    tp = matrix[flood_idx][flood_idx]
    flooded = sum(matrix[flood_idx])
    predicted_flood = sum(row[flood_idx] for row in matrix)
    recall, precision = tp/max(1, flooded), tp/max(1, predicted_flood)
    metrics = {'test_count': len(test), 'flood_count': flooded, 'recall': recall, 'precision': precision,
        'f1': 2*recall*precision/max(1e-12, recall+precision), 'confusion_matrix': matrix,
        'brier_score': sum(brier)/max(1, len(brier)), 'split_by_event': True, 'leakage_detected': False,
        'terminal_central_passed': bool(terminal_passed) and all(terminal_passed),
        'slices': {key: {'recall': item['tp']/max(1, item['tp']+item['fn'])} for key, item in slices.items()}}
    folder = Path(getattr(settings, 'FLOOD_MODEL_REGISTRY_ROOT', '/app/private_flood_models'))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{uuid.uuid4()}.pth'
    torch.save({'model_state_dict': provider.model.state_dict(), 'class_names': classes,
        'config': champion.manifest.get('config', {'model_name': 'resnet50'})}, path)
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    version = FloodModelVersion.objects.create(name=f'candidate-{timezone.now():%Y%m%d-%H%M}',
        artifact_path=str(path), sha256=digest, status='shadow', metrics=metrics,
        manifest={'classes': classes, 'config': champion.manifest.get('config', {'model_name': 'resnet50'}),
            'dataset_sha256': dataset_hash, 'test_events': sorted(test_events),
            'training_event_ids': sorted(set(champion.manifest['training_event_ids']) | (set(events)-test_events)),
            'parent_sha256': champion.sha256, 'epochs': epochs})
    return {'status': 'shadow', 'model_id': str(version.pk), 'metrics': metrics}
