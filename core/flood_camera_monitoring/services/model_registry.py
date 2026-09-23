"""Verified inference providers and explicit, human approved promotion."""
import hashlib
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import transaction

from core.flood_camera_monitoring.infra.models import FloodModelVersion


def verify_artifact(version):
    path = Path(version.artifact_path)
    if not path.is_file():
        raise ValidationError('Model artifact missing')
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if digest != version.sha256:
        raise ValidationError('Model checksum mismatch')
    return path


def load_provider(version):
    path = verify_artifact(version)
    if version.provider == 'torch':
        from core.flood_camera_monitoring.infra.torch_classifier import TorchFloodClassifier
        provider = TorchFloodClassifier(path)
    elif version.provider == 'onnx':
        provider = ONNXProvider(path, version.manifest)
    elif version.provider == 'http':
        provider = RemoteProvider(version.sha256)
    else:
        raise ValidationError('Unsupported inference provider')
    if getattr(provider, '_fallback', False):
        raise ValidationError('Fallback cannot serve operational predictions')
    return provider


class RemoteProvider:
    def __init__(self, version):
        from django.conf import settings
        self.url = getattr(settings, 'FLOOD_REMOTE_PROVIDER_URL', '')
        self.token = getattr(settings, 'FLOOD_REMOTE_PROVIDER_TOKEN', '')
        self.version = version
        if not self.url.startswith('https://'):
            raise ValidationError('Configure an HTTPS inference endpoint')

    def predict(self, image):
        import base64
        import math
        import requests
        from .stream_prediction import PredictionResult, FloodSeverity, FloodProbabilities
        content = image if isinstance(image, bytes) else Path(image).read_bytes()
        response = requests.post(self.url, json={'image_base64': base64.b64encode(content).decode(),
            'model_version': self.version}, headers={'Authorization': f'Bearer {self.token}'}, timeout=(2, 10))
        response.raise_for_status()
        payload = response.json()
        if payload.get('model_version') != self.version:
            raise ValidationError('Remote model version mismatch')
        probabilities = payload['probabilities']
        values = [float(probabilities[key]) for key in ['normal', 'flooded', 'medium']]
        if not all(math.isfinite(value) and 0 <= value <= 100 for value in values) or abs(sum(values)-100) > .1:
            raise ValidationError('Invalid remote model probabilities')
        label = ['normal', 'flooded', 'medium'][values.index(max(values))]
        return PredictionResult(label == 'flooded', FloodSeverity(label), max(values), FloodProbabilities(*values))


class ONNXProvider:
    def __init__(self, path, manifest):
        import onnxruntime
        self.session = onnxruntime.InferenceSession(str(path), providers=['CPUExecutionProvider'])
        self.classes = manifest['classes']
        if sorted(self.classes) not in [sorted(['normal', 'flooded']), sorted(['normal', 'medium', 'flooded'])]:
            raise ValidationError('Invalid model classes')

    def predict(self, image):
        import io
        import numpy as np
        from PIL import Image
        from .stream_prediction import PredictionResult, FloodSeverity, FloodProbabilities
        source = io.BytesIO(image) if isinstance(image, bytes) else image
        with Image.open(source) as frame:
            array = np.asarray(frame.convert('RGB').resize((224, 224)), dtype=np.float32)/255
        array = ((array - np.array([.485, .456, .406], dtype=np.float32)) / np.array([.229, .224, .225], dtype=np.float32)).transpose(2, 0, 1)[None]
        logits = self.session.run(None, {self.session.get_inputs()[0].name: array})[0][0]
        if len(logits) != len(self.classes) or not np.isfinite(logits).all():
            raise ValidationError('Invalid model output')
        probabilities = np.exp(logits-logits.max())
        probabilities = probabilities/probabilities.sum()*100
        values = dict(zip(self.classes, probabilities.tolist()))
        label = self.classes[int(probabilities.argmax())]
        return PredictionResult(label == 'flooded', FloodSeverity(label), float(probabilities.max()),
            FloodProbabilities(values.get('normal', 0), values.get('flooded', 0), values.get('medium', 0)))


def promotion_errors(metrics, baseline=None):
    errors = []
    if metrics.get('test_count', 0) < 100 or metrics.get('flood_count', 0) < 30:
        errors.append('Insufficient held-out dataset: require 100 examples including 30 floods')
    if not metrics.get('split_by_event') or metrics.get('leakage_detected', True):
        errors.append('Event-isolated split and leakage audit required')
    if metrics.get('recall', 0) < max(.95, (baseline or {}).get('recall', 0)):
        errors.append('Flood recall regression or recall below 95%')
    if metrics.get('precision', 0) < .9:
        errors.append('Precision below 90%')
    if not metrics.get('terminal_central_passed'):
        errors.append('Terminal Central regression missing or failing')
    if not metrics.get('slices'):
        errors.append('Camera and environmental slice evaluation required')
    if set((baseline or {}).get('slices', {})) - set(metrics.get('slices', {})):
        errors.append('Candidate evaluation omits baseline slices')
    for name, values in metrics.get('slices', {}).items():
        previous = (baseline or {}).get('slices', {}).get(name, {})
        if values.get('recall', 0) < max(.9, previous.get('recall', 0)):
            errors.append(f'Slice recall regression: {name}')
    return errors


def promote_model(version_id, actor):
    if not actor or not actor.is_active or not (actor.is_superuser or actor.is_staff or actor.type == 'admin'):
        raise ValidationError('Active administrator required')
    with transaction.atomic():
        # Serialize promotion even when no champion exists.
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [84710301])
        candidate = FloodModelVersion.objects.select_for_update().get(pk=version_id)
        if candidate.status not in ('shadow', 'retired'):
            raise ValidationError('Candidate must complete shadow evaluation first')
        from core.flood_camera_monitoring.infra.models import ModelShadowResult
        if ModelShadowResult.objects.filter(model=candidate, error_code='').count() < 30:
            raise ValidationError('At least 30 successful shadow windows required')
        champion = FloodModelVersion.objects.filter(status='champion').first()
        errors = promotion_errors(candidate.metrics, champion.metrics if champion else None)
        if errors:
            raise ValidationError(errors)
        load_provider(candidate)
        FloodModelVersion.objects.filter(status='champion').update(status='retired')
        candidate.status = 'champion'
        candidate.approved_by = actor
        candidate.save(update_fields=['status', 'approved_by'])
        return candidate
