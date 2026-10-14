"""Small resumable CPU training segments. Only manually created jobs are processed."""
import hashlib
import math
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone
from core.flood_camera_monitoring.infra.models import TrainingJob, ModelEvaluation, FloodModelVersion
from .learning_data import lock, readiness, dataset_current, safe_path, curated_root, storage_usage
from .model_lifecycle import operational_version, compatible, budget_ready

LABEL_MAP = {'NO_INDICATION':'normal', 'INTERMEDIATE_INDICATION':'medium', 'FLOOD_INDICATION':'flooded'}
TERMINAL = ('COMPLETED', 'CANCELLED', 'FAILED')


def provenance_errors(dataset, parent):
    rows = dataset.manifest['rows']
    errors = []
    held = {r['event_id'] for r in rows if r['split'] in ('test', 'validation')}
    if held & set(parent.manifest.get('training_event_ids', [])):
        errors.append('O modelo de origem já treinou em eventos reservados para avaliação.')
    held_hashes = {r['sha256'] for r in rows if r['split'] != 'train'}
    if held_hashes & set(parent.manifest.get('training_image_hashes', [])):
        errors.append('O modelo de origem já treinou em imagens reservadas para avaliação.')
    cutoff = parent.manifest.get('prospective_after')
    if parent.manifest.get('provenance') == 'unknown' and not cutoff:
        errors.append('Registre a data de início da avaliação prospectiva do modelo de origem.')
    if cutoff and any(datetime.fromisoformat(r['captured_at']) <= datetime.fromisoformat(cutoff) for r in rows if r['split'] != 'train'):
        errors.append('A avaliação deve utilizar episódios posteriores ao registro prospectivo do modelo.')
    return errors


@transaction.atomic
def request_training(dataset, actor, request_id, epochs=3):
    lock(84710303)
    old = TrainingJob.objects.filter(request_id=request_id).first()
    if old:
        if old.dataset_id != dataset.id or old.actor_id != actor.id: raise ValidationError('Chave já utilizada por outro treinamento.')
        return old
    if TrainingJob.objects.exclude(status__in=TERMINAL).exists(): raise ValidationError('Já existe um treinamento pendente ou em execução.')
    parent = operational_version()
    if not parent or parent.provider != 'torch': raise ValidationError('Registre o modelo Torch operacional primeiro.')
    errors = readiness(dataset.manifest['rows'])['blockers'] + provenance_errors(dataset, parent)
    if not dataset_current(dataset): errors.append('O conjunto foi superado por revisões ou vínculos de eventos; gere uma nova versão.')
    if errors: raise ValidationError(errors)
    return TrainingJob.objects.create(dataset=dataset, actor=actor, parent=parent, request_id=request_id,
        config={'epochs': max(1,min(int(epochs),10)), 'seed':42, 'learning_rate':.0001, 'threads':1})


@transaction.atomic
def request_evaluation(dataset, version, actor, request_id):
    lock(84710303)
    old = TrainingJob.objects.filter(request_id=request_id).first()
    if old:
        if old.dataset_id != dataset.id or old.candidate_id != version.id or old.actor_id != actor.id:
            raise ValidationError('Chave já utilizada em outra avaliação.')
        return old
    if TrainingJob.objects.exclude(status__in=TERMINAL).exists():
        raise ValidationError('Já existe um treinamento ou avaliação em andamento.')
    parent = operational_version()
    if not parent or parent.provider != 'torch' or version.provider != 'torch':
        raise ValidationError('A comparação local requer modelos Torch.')
    errors = readiness(dataset.manifest['rows'])['blockers'] + provenance_errors(dataset,parent) + provenance_errors(dataset,version)
    if not dataset_current(dataset): errors.append('As revisões do conjunto foram superadas.')
    if errors: raise ValidationError(errors)
    return TrainingJob.objects.create(dataset=dataset,actor=actor,parent=parent,candidate=version,request_id=request_id,
        config={'mode':'evaluation','epochs':0,'seed':42,'learning_rate':.0001,'threads':1})


@transaction.atomic
def control_job(job_id, action):
    lock(84710303)
    job = TrainingJob.objects.select_for_update().get(pk=job_id)
    if action not in ('pause','resume','cancel'): raise ValidationError('Ação inválida.')
    if job.status in TERMINAL: raise ValidationError('O treinamento já foi encerrado.')
    if action == 'resume' and job.status not in ('PAUSED', 'WAITING_CAPACITY', 'WAITING_DATA'):
        raise ValidationError('O treinamento já está em execução.')
    job.requested_action = action
    if action == 'resume': job.status = 'WAITING_CAPACITY'
    elif action == 'pause': job.status = 'PAUSED'
    elif action == 'cancel': job.status = 'CANCELLED'
    job.updated_at = timezone.now()
    job.save(update_fields=['requested_action','status','updated_at'])
    return job


def wilson(success, count):
    if not count: return None
    p, z = success/count, 1.96
    center = (p+z*z/(2*count))/(1+z*z/count)
    half = z*math.sqrt(p*(1-p)/count+z*z/(4*count*count))/(1+z*z/count)
    return [max(0,center-half), min(1,center+half)]


def metrics(rows, predictions, classes, durations):
    matrix = [[0]*len(classes) for _ in classes]
    slices, bins = {}, [{'count':0,'sum_probability':0.,'floods':0} for _ in range(10)]
    fi = classes.index('flooded')
    terminal = []
    brier = 0
    for row, probabilities in zip(rows, predictions):
        actual = classes.index(LABEL_MAP[row['classification']])
        predicted = max(range(len(classes)), key=lambda i: probabilities[i])
        matrix[actual][predicted] += 1
        probability = probabilities[fi]
        brier += (probability-int(actual==fi))**2
        bucket = bins[min(9,int(probability*10))]
        bucket['count'] += 1; bucket['sum_probability'] += probability; bucket['floods'] += int(actual==fi)
        local = datetime.fromisoformat(row['captured_at']).astimezone(ZoneInfo(settings.FLOOD_REVIEW_TIMEZONE))
        for key in [row['camera_id'], 'night' if local.hour < 6 or local.hour >= 18 else 'day']:
            item = slices.setdefault(key, {'count':0,'floods':0,'tp':0})
            item['count'] += 1; item['floods'] += int(actual==fi); item['tp'] += int(actual==fi and predicted==fi)
        if actual==fi and 'terminal central' in row['camera_name'].lower(): terminal.append(predicted==fi)
    class_metrics = {}
    for i, name in enumerate(classes):
        tp, actual, predicted = matrix[i][i], sum(matrix[i]), sum(r[i] for r in matrix)
        precision = tp/predicted if predicted else None
        recall = tp/actual if actual else None
        class_metrics[name] = {'precision':precision,'recall':recall,'f1':2*tp/(actual+predicted) if actual+predicted else None,
            'precision_interval':wilson(tp,predicted),'recall_interval':wilson(tp,actual),'support':actual}
    flood = class_metrics['flooded']
    import resource
    return {'worker_peak_memory_mib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        'test_count':len(rows),'flood_count':flood['support'],'precision':flood['precision'],'recall':flood['recall'],
        'classes':classes,'per_class':class_metrics,'confusion_matrix':matrix,'brier_score':brier/len(rows) if rows else None,
        'calibration':bins,'split_by_event':True,'leakage_detected':False,'terminal_central_passed':bool(terminal) and all(terminal),
        'slices':{key:{**v,'recall':v['tp']/v['floods'],'recall_interval':wilson(v['tp'],v['floods'])} for key,v in slices.items() if v['floods']>=5},
        'slice_samples':slices,'latency_ms':sum(durations)/len(durations) if durations else None,
        'period': [min(r['captured_at'] for r in rows),max(r['captured_at'] for r in rows)] if rows else [],
        'metric_scope':'held_out_test', 'uncertainty':'Wilson 95%; amostras do mesmo evento não são independentes'}


def save_checkpoint(path, payload):
    import torch
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Account for the temporary copy as well as the old checkpoint.
    estimate = sum(v.nelement()*v.element_size() for v in payload['weights'].values()) * 2 + 1024**2
    if storage_usage()['models']['bytes'] + estimate > settings.FLOOD_MODEL_LIMIT_BYTES:
        raise ValidationError('MODEL_STORAGE_LIMIT')
    temporary = path.with_suffix('.tmp')
    try:
        torch.save(payload, temporary)
        temporary.chmod(0o600)
        if storage_usage()['models']['bytes'] > settings.FLOOD_MODEL_LIMIT_BYTES:
            raise ValidationError('MODEL_STORAGE_LIMIT')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def process_next_job():
    """Session advisory lock fences duplicate Celery deliveries across segment commits."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s)', [84710304])
        if not cursor.fetchone()[0]: return
    try:
        job = TrainingJob.objects.exclude(status__in=TERMINAL).exclude(requested_action='pause').select_related('dataset','parent').order_by('created_at').first()
        if not job: return
        if not budget_ready(start=job.status not in ('TRAINING','EVALUATING')):
            TrainingJob.objects.filter(pk=job.pk).exclude(requested_action__in=['pause','cancel']).update(status='WAITING_CAPACITY', updated_at=timezone.now())
            return
        try:
            _segment(job)
        except Exception as exc:
            TrainingJob.objects.filter(pk=job.pk).exclude(requested_action='cancel').update(status='FAILED', error_code=(exc.messages[0][:100] if isinstance(exc, ValidationError) else type(exc).__name__), updated_at=timezone.now())
            import logging
            logging.getLogger(__name__).exception('Training segment failed for job %s', job.pk)
    finally:
        with connection.cursor() as cursor: cursor.execute('SELECT pg_advisory_unlock(%s)', [84710304])


def _segment(job):
    import torch
    from PIL import Image
    torch.set_num_threads(1)
    started = time.monotonic()
    path = Path(settings.FLOOD_MODEL_REGISTRY_ROOT) / f'job-{job.pk}.pth'
    if not path.exists() and not dataset_current(job.dataset):
        TrainingJob.objects.filter(pk=job.pk).update(status='WAITING_DATA', error_code='DATASET_SUPERSEDED')
        return
    provider = compatible(job.candidate if job.config.get('mode') == 'evaluation' else job.parent)
    classes = provider.class_names
    head = getattr(getattr(provider.model,'backbone',None),'fc',None)
    if head is None: raise ValidationError('Arquitetura sem camada classificadora compatível.')
    for p in provider.model.parameters(): p.requires_grad = False
    for p in head.parameters(): p.requires_grad = True
    optimizer = torch.optim.Adam(head.parameters(),lr=job.config['learning_rate'])
    torch.manual_seed(job.config['seed'])
    checkpoint = {'phase':'candidate' if job.config.get('mode') == 'evaluation' else 'train','epoch':0,'index':0,'predictions':[], 'durations':[], 'validation_loss':[], 'cpu_seconds':0.}
    if path.exists():
        checkpoint = torch.load(path,map_location='cpu',weights_only=True)
        provider.model.load_state_dict(checkpoint['weights'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        torch.set_rng_state(checkpoint['rng'])
    provider.model.eval()
    rows = job.dataset.manifest['rows']
    train = [r for r in rows if r['split']=='train']
    validation = [r for r in rows if r['split']=='validation']
    test = [r for r in rows if r['split']=='test']
    baseline_provider = None
    cpu_start = time.process_time()
    while time.monotonic()-started < 18:
        job.refresh_from_db(fields=['requested_action','status'])
        if job.requested_action in ('pause','cancel'): break
        if not budget_ready():
            job.status = 'WAITING_CAPACITY'
            break
        phase = checkpoint['phase']
        selected = train if phase=='train' else validation if phase=='validation' else test
        if checkpoint['index'] >= len(selected):
            checkpoint['index'] = 0
            if phase=='train': checkpoint['phase'] = 'validation'
            elif phase=='validation':
                checkpoint['epoch'] += 1
                checkpoint['phase'] = 'train' if checkpoint['epoch'] < job.config['epochs'] else 'candidate'
            elif phase=='candidate':
                _record_evaluation(job, provider, checkpoint, test, classes)
                checkpoint['phase'],checkpoint['predictions'],checkpoint['durations'] = 'baseline',[],[]
            else:
                result = metrics(test,checkpoint['predictions'],classes,checkpoint['durations'])
                result['dataset_id'] = str(job.dataset_id)
                ModelEvaluation.objects.get_or_create(dataset=job.dataset,model=job.parent,defaults={'metrics':result})
                checkpoint['phase'] = 'complete'
                break
            continue
        row = selected[checkpoint['index']]
        content = safe_path(curated_root(), row['path']).read_bytes()
        if hashlib.sha256(content).hexdigest() != row['sha256']: raise ValidationError('CURATED_HASH_MISMATCH')
        import io
        with Image.open(io.BytesIO(content)) as image:
            tensor = provider.transform(image.convert('RGB')).unsqueeze(0)
        if phase=='train':
            optimizer.zero_grad()
            logits = provider.model(tensor)
            loss = torch.nn.functional.cross_entropy(logits,torch.tensor([classes.index(LABEL_MAP[row['classification']])]))
            loss.backward(); optimizer.step()
        else:
            if phase=='baseline' and baseline_provider is None: baseline_provider = compatible(job.parent)
            model = baseline_provider.model if phase=='baseline' else provider.model
            before = time.monotonic()
            with torch.inference_mode():
                logits = model(tensor)
                if phase=='validation':
                    loss = torch.nn.functional.cross_entropy(logits,torch.tensor([classes.index(LABEL_MAP[row['classification']])]))
                    checkpoint['validation_loss'].append(float(loss))
                else:
                    probabilities = torch.softmax(logits,dim=1)[0].tolist()
                    if phase == 'baseline':
                        probabilities = [probabilities[baseline_provider.class_names.index(name)] for name in classes]
                    checkpoint['predictions'].append(probabilities)
                    checkpoint['durations'].append((time.monotonic()-before)*1000)
        checkpoint['index'] += 1
        job.status = 'TRAINING' if phase in ('train','validation') else 'EVALUATING'
    checkpoint.update(weights=provider.model.state_dict(), optimizer=optimizer.state_dict(),rng=torch.get_rng_state())
    checkpoint['cpu_seconds'] += time.process_time()-cpu_start
    save_checkpoint(path,checkpoint)
    with transaction.atomic():
        locked = TrainingJob.objects.select_for_update().get(pk=job.pk)
        if locked.requested_action == 'cancel': locked.status = 'CANCELLED'
        elif locked.requested_action == 'pause': locked.status = 'PAUSED'
        else:
            locked.status = 'COMPLETED' if checkpoint['phase']=='complete' else job.status
            locked.requested_action = ''
        locked.progress = {k:checkpoint[k] for k in ('phase','epoch','index','cpu_seconds')}
        locked.updated_at = timezone.now()
        locked.save(update_fields=['status','requested_action','progress','updated_at'])


def _record_evaluation(job, provider, checkpoint, rows, classes):
    import torch
    if job.candidate_id is None:
        folder = Path(settings.FLOOD_MODEL_REGISTRY_ROOT)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = folder / f'candidate-{job.pk}.pth'
        payload = {'model_state_dict':provider.model.state_dict(),'class_names':classes,'config':job.parent.manifest.get('config',{'model_name':'resnet50'})}
        size = sum(v.nelement()*v.element_size() for v in payload['model_state_dict'].values())+1024**2
        if storage_usage()['models']['bytes']+size > settings.FLOOD_MODEL_LIMIT_BYTES: raise ValidationError('MODEL_STORAGE_LIMIT')
        temporary = target.with_suffix('.tmp')
        torch.save(payload,temporary)
        temporary.chmod(0o600)
        os.replace(temporary,target)
        with target.open('rb') as source: digest = hashlib.file_digest(source,'sha256').hexdigest()
        manifest = {'classes':classes,'config':payload['config'],'dataset_id':str(job.dataset_id),'parent_sha256':job.parent.sha256,
            'training_event_ids':sorted(set(job.parent.manifest.get('training_event_ids',[]))|{r['event_id'] for r in job.dataset.manifest['rows'] if r['split']=='train'}),
            'training_image_hashes':sorted(set(job.parent.manifest.get('training_image_hashes',[]))|{r['sha256'] for r in job.dataset.manifest['rows'] if r['split']=='train'}),
            'provenance':job.parent.manifest.get('provenance','unknown'),'prospective_after':job.parent.manifest.get('prospective_after'),
            'training_config':job.config}
        candidate, _ = FloodModelVersion.objects.get_or_create(sha256=digest,defaults={'name':f'Candidato {str(job.pk)[:8]}','artifact_path':str(target),'manifest':manifest})
        TrainingJob.objects.filter(pk=job.pk).update(candidate=candidate)
        job.candidate = candidate
    result = metrics(rows,checkpoint['predictions'],classes,checkpoint['durations'])
    result['dataset_id'] = str(job.dataset_id)
    ModelEvaluation.objects.get_or_create(dataset=job.dataset,model=job.candidate,defaults={'metrics':result})
