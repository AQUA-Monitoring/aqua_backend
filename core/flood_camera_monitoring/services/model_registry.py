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
        errors.append('O teste precisa de 100 exemplos, incluindo 30 de alagamento.')
    if not metrics.get('split_by_event') or metrics.get('leakage_detected', True):
        errors.append('É necessária separação por evento e verificação de vazamento de dados.')
    if (metrics.get('recall') or 0) < max(.95, (baseline or {}).get('recall') or 0):
        errors.append('Sensibilidade para alagamento abaixo de 95% ou inferior à referência.')
    if (metrics.get('precision') or 0) < .9:
        errors.append('Precisão para alagamento abaixo de 90%.')
    if not metrics.get('terminal_central_passed'):
        errors.append('Cenário obrigatório do Terminal Central ausente ou com regressão.')
    if not metrics.get('slices'):
        errors.append('Faltam recortes por câmera e período com amostra suficiente.')
    if set((baseline or {}).get('slices', {})) - set(metrics.get('slices', {})):
        errors.append('A avaliação omite recortes presentes na referência.')
    for name, values in metrics.get('slices', {}).items():
        previous = (baseline or {}).get('slices', {}).get(name, {})
        if values.get('recall', 0) < max(.9, previous.get('recall', 0)):
            errors.append(f'Regressão de sensibilidade no recorte: {name}')
    return errors


def promote_model(version_id, actor):
    # Legacy endpoint uses the same comparison and pilot gates as the new panel.
    import uuid
    from .model_lifecycle import deploy
    if not actor or not actor.is_active or not (actor.is_superuser or actor.is_staff or actor.type == 'admin'):
        raise ValidationError('Active administrator required')
    deployment = deploy(version_id, actor, 'EXPAND', 'Aplicação pelo endpoint administrativo legado', uuid.uuid4())
    return deployment.target
