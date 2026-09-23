from core.flood_camera_monitoring.infra.models import CameraAnalysisRun, FloodModelVersion, ModelShadowResult
from .adaptive_monitoring import evidence_root
from .model_registry import load_provider


def evaluate_shadow(run_id):
    run = CameraAnalysisRun.objects.get(pk=run_id)
    for version in FloodModelVersion.objects.filter(status='shadow')[:2]:
        if ModelShadowResult.objects.filter(run=run, model=version).exists():
            continue
        values, error = [], ''
        try:
            provider = load_provider(version)
            for evidence in run.evidence.filter(deleted_at__isnull=True).order_by('captured_at'):
                path = (evidence_root()/evidence.relative_path).resolve()
                if not path.is_relative_to(evidence_root()):
                    raise ValueError('Invalid evidence path')
                result = provider.predict(path.read_bytes())
                values.append({'normal': result.probabilities.normal, 'medium': result.probabilities.medium,
                    'flooded': result.probabilities.flooded})
            if not values:
                error = 'NO_EVIDENCE'
        except Exception as exc:
            error = type(exc).__name__
        ModelShadowResult.objects.get_or_create(run=run, model=version,
            defaults={'probabilities': values, 'error_code': error})
