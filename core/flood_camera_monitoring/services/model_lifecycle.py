"""Explicit, audited model deployments; parallel evaluation is capacity bounded."""
import hashlib
import time
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from core.flood_camera_monitoring.infra.models import (FloodModelVersion, ModelAssignment, ModelDeployment,
    ModelEvaluation, ShadowSession, ShadowWindow, ModelShadowResult, CameraAnalysisRun, CameraMonitoringState,
    TrainingJob, Camera)
from .learning_data import lock, CLASSES
from .model_registry import load_provider, promotion_errors


def operational_version(camera_id=None):
    assignment = ModelAssignment.objects.select_related('model').filter(camera_id=camera_id).first() if camera_id else None
    return assignment.model if assignment else FloodModelVersion.objects.filter(status='champion').first()


def compatible(version):
    provider = load_provider(version)
    classes = getattr(provider, 'class_names', getattr(provider, 'classes', version.manifest.get('classes', [])))
    if set(classes) != {'normal', 'medium', 'flooded'}:
        raise ValidationError('O modelo deve preservar as três classes operacionais.')
    return provider


@transaction.atomic
def bootstrap(actor, request_id):
    lock(84710301)
    previous = ModelDeployment.objects.filter(request_id=request_id).first()
    if previous:
        if previous.actor_id != actor.id or previous.action != 'BOOTSTRAP':
            raise ValidationError('Chave de idempotência utilizada por outra ação.')
        return previous.target
    if operational_version(): raise ValidationError('Já existe um modelo operacional registrado.')
    from core.flood_camera_monitoring.infra.utils import resolve_checkpoint_path
    path = resolve_checkpoint_path().resolve()
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    version = FloodModelVersion(name='Modelo operacional inicial', artifact_path=str(path), sha256=digest)
    provider = load_provider(version)
    classes = provider.class_names
    if set(classes) != {'normal', 'medium', 'flooded'}: raise ValidationError('Classes incompatíveis.')
    version.manifest = {'classes': classes, 'architecture': type(provider.model).__name__,
        'config': getattr(provider, 'checkpoint_config', {}), 'provenance': 'unknown',
        'prospective_after': timezone.now().isoformat(), 'origin': 'checkpoint operacional configurado'}
    version.status = 'champion'
    version.approved_by = actor
    version.save()
    ModelDeployment.objects.create(request_id=request_id, actor=actor, target=version, action='BOOTSTRAP',
        reason='Registro do checkpoint já operacional; histórico de treinamento desconhecido.')
    return version


def comparison(version, current_rows=None):
    champion = operational_version()
    evaluation = ModelEvaluation.objects.filter(model=version).select_related('dataset').order_by('-created_at').first()
    baseline = ModelEvaluation.objects.filter(model=champion, dataset=evaluation.dataset).first() if evaluation and champion else None
    blockers = []
    if not evaluation or not baseline:
        blockers.append('Avalie candidato e modelo operacional no mesmo conjunto de teste.')
    else:
        from .learning_data import dataset_current
        if not dataset_current(evaluation.dataset,current_rows):
            blockers.append('O teste contém revisões ou vínculos de eventos superados; gere uma nova avaliação.')
        blockers.extend(promotion_errors(evaluation.metrics, baseline.metrics))
    session = ShadowSession.objects.filter(model=version).first()
    windows = ModelShadowResult.objects.filter(model=version, error_code='', created_at__gte=session.started_at).count() if session else 0
    if windows < 30: blockers.append(f'Faltam {30-windows} janelas válidas de avaliação paralela.')
    return {'dataset_id': str(evaluation.dataset_id) if evaluation else None,
        'candidate': evaluation.metrics if evaluation else None, 'baseline': baseline.metrics if baseline else None,
        'baseline_id': str(champion.pk) if champion else None, 'shadow_active': bool(session and session.active), 'shadow_windows': windows, 'blockers': blockers}


def pilot_errors(version):
    assignments = ModelAssignment.objects.filter(model=version)
    errors = []
    if not assignments.exists(): return ['Inicie um piloto em uma câmera.']
    for assignment in assignments:
        if timezone.now() - assignment.started_at < timedelta(hours=24): errors.append('O piloto precisa completar 24 horas.')
        runs = CameraAnalysisRun.objects.filter(camera_id=assignment.camera_id, model_version=version.sha256, started_at__gte=assignment.started_at)
        ordered = list(runs.order_by('started_at').values_list('started_at', flat=True))
        if not ordered or (ordered[0]-assignment.started_at).total_seconds() > 600 or (timezone.now()-ordered[-1]).total_seconds() > 600 or any((b-a).total_seconds() > 600 for a,b in zip(ordered, ordered[1:])):
            errors.append('Piloto sem cobertura contínua de análises (lacunas acima de dez minutos).')
        if runs.exclude(error_code='').exists(): errors.append('O piloto registrou erros; revise antes de expandir.')
        if runs.filter(latency_ms__gt=110000).exists(): errors.append('O piloto excedeu o orçamento de latência.')
    return sorted(set(errors))


@transaction.atomic
def deploy(version_id, actor, action, reason, request_id, camera_id=None):
    if not reason.strip(): raise ValidationError('Informe a justificativa da aplicação.')
    lock(84710301)
    existing = ModelDeployment.objects.filter(request_id=request_id).first()
    if existing:
        if str(existing.target_id) != str(version_id) or existing.actor_id != actor.id or existing.action != action or str(existing.camera_id or '') != str(camera_id or '') or existing.reason != reason:
            raise ValidationError('Chave de idempotência utilizada por outra aplicação.')
        return existing
    target = FloodModelVersion.objects.get(pk=version_id)
    previous = operational_version(camera_id)
    if action in ('PILOT', 'EXPAND'):
        errors = comparison(target)['blockers']
        if action == 'EXPAND': errors += pilot_errors(target)
        if errors: raise ValidationError(errors)
    elif action == 'ROLLBACK':
        if not ModelDeployment.objects.filter(target=target, action__in=['BOOTSTRAP','PILOT','EXPAND','ROLLBACK']).exists():
            raise ValidationError('Essa versão nunca foi operacional.')
    elif action == 'GLOBAL_FALLBACK':
        if not camera_id or target != operational_version(): raise ValidationError('Selecione a versão global e uma câmera piloto.')
    else: raise ValidationError('Ação de aplicação inválida.')
    compatible(target)
    if action == 'PILOT' and (not camera_id or ModelAssignment.objects.exists()):
        raise ValidationError('Selecione uma câmera; somente um piloto pode permanecer ativo.')
    if camera_id:
        Camera.objects.get(pk=camera_id, status=1)
        if action == 'GLOBAL_FALLBACK': ModelAssignment.objects.filter(camera_id=camera_id).delete()
        else: ModelAssignment.objects.update_or_create(camera_id=camera_id, defaults={'model': target, 'started_at': timezone.now()})
    else:
        if action == 'PILOT': raise ValidationError('Câmera piloto obrigatória.')
        FloodModelVersion.objects.filter(status='champion').exclude(pk=target.pk).update(status='retired')
        target.status, target.approved_by = 'champion', actor
        target.save(update_fields=['status', 'approved_by'])
        ModelAssignment.objects.all().delete()
        ShadowSession.objects.filter(model=target, active=True).update(active=False)
    return ModelDeployment.objects.create(request_id=request_id, actor=actor, camera_id=camera_id,
        previous=previous, target=target, action=action, reason=reason)


def budget_ready(start=False):
    from .monitoring_capacity import client, KEY
    import json
    try:
        raw = client().get(KEY)
        state = json.loads(raw) if raw else {}
        now = time.time()
        if not 0 <= now - state.get('sampled_at', 0) <= 30 or state.get('cpu_percent') is None: return False
        if state['cpu_percent'] >= 70 or state['available_bytes'] <= 2 * 1024**3 or state.get('restricted', False): return False
        if start and (state['cpu_percent'] >= 55 or state['available_bytes'] <= 3 * 1024**3
                      or state.get('healthy_since') is None or now - state['healthy_since'] < 300): return False
        return not CameraMonitoringState.objects.filter(level='CRITICAL', camera__status=1).exists()
    except (ValueError, TypeError, KeyError, OSError): return False
    except Exception: return False  # Redis outage is unavailable capacity, never permission to train.


@transaction.atomic
def start_shadow(version_id, actor=None, request_id=None):
    lock(84710301)
    previous = ModelDeployment.objects.filter(request_id=request_id).first() if request_id else None
    if previous:
        if previous.actor_id != actor.id or previous.target_id != version_id or previous.action != 'SHADOW':
            raise ValidationError('Chave de idempotência utilizada por outra ação.')
        return ShadowSession.objects.get(model_id=version_id)
    version = FloodModelVersion.objects.get(pk=version_id)
    if version.status == 'champion': raise ValidationError('Selecione um candidato.')
    compatible(version)
    if not ModelEvaluation.objects.filter(model=version).exists(): raise ValidationError('Avalie o candidato no conjunto de teste primeiro.')
    ShadowSession.objects.exclude(model=version).update(active=False)
    session, _ = ShadowSession.objects.get_or_create(model=version)
    session.active = True
    session.save()
    if actor and request_id:
        ModelDeployment.objects.get_or_create(request_id=request_id, defaults={'actor': actor, 'target': version, 'action': 'SHADOW', 'reason': 'Avaliação paralela solicitada pelo administrador.'})
    return session


@transaction.atomic
def reserve_shadow(run_id):
    lock(84710303)
    session = ShadowSession.objects.filter(active=True).first()
    if not session or ShadowWindow.objects.filter(run_id=run_id).exists(): return False
    run = CameraAnalysisRun.objects.get(pk=run_id)
    if not run.context.get('quality_usable') or run.error_code or run.model_version == session.model.sha256: return False
    # Exact upper bound: at most one of each ten eligible observations, including
    # skipped opportunities. Retried deliveries never advance this counter.
    window = ShadowWindow.objects.create(run=run, session=session, status='SKIPPED')
    if ShadowWindow.objects.filter(session=session).count() % 10: return False
    if not budget_ready(start=True): return False
    if TrainingJob.objects.exclude(status__in=['COMPLETED','FAILED','CANCELLED']).exists(): return False
    ShadowWindow.objects.filter(status__in=['PENDING','RUNNING'], created_at__lt=timezone.now()-timedelta(seconds=120)).update(status='EXPIRED')
    if ShadowWindow.objects.filter(status__in=['PENDING','RUNNING']).exists(): return False
    if ShadowWindow.objects.filter(session=session, run__camera_id=run.camera_id,
            status__in=['PENDING','RUNNING','COMPLETED','FAILED'], created_at__gte=timezone.now()-timedelta(minutes=5)).exists(): return False
    window.status = 'PENDING'
    window.save(update_fields=['status'])
    return True
