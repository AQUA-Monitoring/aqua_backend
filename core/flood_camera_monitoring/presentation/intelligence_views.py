"""Administrative intelligence API. GET requests only read persisted state."""
import uuid
from django.core import signing
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.db.models import Exists, OuterRef
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import serializers
from core.users.permissions import IsAppAdmin
from core.flood_camera_monitoring.infra.models import (ReviewEpisode, EpisodeImage, ImageLabel, LearningDataset,
    TrainingJob, FloodModelVersion, ModelDeployment, ModelAssignment, Camera, CuratedImage)
from core.flood_camera_monitoring.services import learning_data as data
from core.flood_camera_monitoring.services import learning_training as training
from core.flood_camera_monitoring.services import model_lifecycle as lifecycle


class AdminView(APIView):
    permission_classes = [IsAppAdmin]
    def check_permissions(self, request):
        super().check_permissions(request)
        if not request.user.is_active:
            self.permission_denied(request)

    def handle_exception(self, exc):
        if isinstance(exc, ValidationError): return Response({'detail':exc.messages},status=400)
        if isinstance(exc, ObjectDoesNotExist): return Response({'detail':'Registro não encontrado.'},status=404)
        return super().handle_exception(exc)


class DecisionInput(serializers.Serializer):
    classification = serializers.ChoiceField(choices=data.LABELS)
    reason = serializers.CharField(max_length=2000)
    request_id = serializers.UUIDField()
    previous_id = serializers.UUIDField(required=False, allow_null=True)


class ActionInput(serializers.Serializer):
    action = serializers.ChoiceField(choices=['bootstrap','shadow','evaluate','PILOT','EXPAND','ROLLBACK','GLOBAL_FALLBACK'])
    request_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000,required=False,default='')
    camera_id = serializers.UUIDField(required=False,allow_null=True)
    dataset_id = serializers.UUIDField(required=False)


def paginate(query, request, render):
    try: page = max(1,int(request.query_params.get('page',1)))
    except ValueError: raise ValidationError('Página inválida.')
    count = query.count()
    return {'count':count,'page':page,'page_size':20,'results':[render(item) for item in query[(page-1)*20:page*20]]}


def image_payload(selection, user):
    label = selection.labels.order_by('-created_at','-id').first()
    token = signing.dumps({'image':selection.image_id,'user':str(user.pk)},salt='curated-image')
    return {'id':str(selection.pk),'sha256':selection.image_id,'revision':selection.revision,
        'run_id':str(selection.evidence.run_id),'captured_at':selection.evidence.captured_at,
        'prediction':selection.prediction,'classification':selection.classification,
        'url':f'/api/flood_monitoring/intelligence/images/{selection.image_id}/?token={token}',
        'label': {'id':str(label.pk),'classification':label.classification,'reason':label.reason,'created_at':label.created_at,'actor_id':str(label.actor_id)} if label else None}


def episode_payload(episode, user):
    images = list(episode.images.filter(revision=episode.revision).select_related('evidence').prefetch_related('labels'))
    return {'id':str(episode.pk),'camera_id':str(episode.camera_id),'camera_name':episode.camera.description,
        'model_version':episode.model_version,'event_id':str(episode.event_id),'kind':episode.kind,'reason':episode.reason,
        'started_at':episode.started_at,'last_at':episode.last_at,'closed_at':episode.closed_at,
        'observed_seconds':episode.observed_seconds,'minimum_normal':episode.minimum_normal,'latest_normal':episode.latest_normal,
        'revision':episode.revision,'images':[image_payload(s,user) for s in images]}


class EpisodesView(AdminView):
    def get(self,request):
        query = ReviewEpisode.objects.filter(visible=True).select_related('camera').order_by('-last_at')
        query = query.filter(kind='audit' if request.query_params.get('kind')=='audit' else 'divergence')
        if request.query_params.get('camera'):
            try: query = query.filter(camera_id=uuid.UUID(request.query_params['camera']))
            except ValueError: raise ValidationError('Câmera inválida.')
        if request.query_params.get('reason'): query = query.filter(reason=request.query_params['reason'])
        for key, lookup in [('from','last_at__gte'),('to','started_at__lte')]:
            if request.query_params.get(key):
                value = parse_datetime(request.query_params[key])
                if value is None: raise ValidationError('Período inválido.')
                query = query.filter(**{lookup:value})
        selections = EpisodeImage.objects.filter(episode_id=OuterRef('pk'),revision=OuterRef('revision'),labels__isnull=True)
        query = query.annotate(pending=Exists(selections))
        status = request.query_params.get('review')
        if status in ('pending','reviewed'): query = query.filter(pending=status=='pending')
        from django.db.models import Case, When, IntegerField
        query = query.annotate(priority=Case(When(reason='STRONG',then=0),When(reason='PERSISTENT',then=1),default=2,output_field=IntegerField())).order_by('priority','-last_at')
        return Response(paginate(query,request,lambda e:episode_payload(e,request.user)))


class EpisodeView(AdminView):
    def get(self,request,episode_id):
        episode = get_object_or_404(ReviewEpisode,pk=episode_id)
        result = episode_payload(episode,request.user)
        result['history'] = list(episode.observations.order_by('-run__started_at').values('run_id','run__started_at','valid','normal')[:100])
        result['revisions'] = [image_payload(s,request.user) for s in episode.images.select_related('evidence').order_by('-revision')[:100]]
        return Response(result)
    def post(self,request,episode_id):
        try: event = uuid.UUID(str(request.data.get('event_id','')))
        except ValueError: raise ValidationError('Informe o identificador do evento a vincular.')
        data.link_event(episode_id,event,request.user)
        return Response({'event_id':str(event)})


class LabelView(AdminView):
    def post(self,request,selection_id):
        form = DecisionInput(data=request.data); form.is_valid(raise_exception=True)
        label = data.label_image(selection_id,request.user,form.validated_data['classification'],form.validated_data['reason'],
            form.validated_data['request_id'],form.validated_data.get('previous_id'))
        return Response({'id':label.pk,'classification':label.classification},status=201)


class CuratedImageView(AdminView):
    def get(self,request,digest):
        try: token = signing.loads(request.query_params.get('token',''),salt='curated-image',max_age=300)
        except signing.BadSignature: return Response({'detail':'Link expirado.'},status=403)
        if token != {'image':digest,'user':str(request.user.pk)}: return Response(status=403)
        item = get_object_or_404(CuratedImage,pk=digest)
        path = data.safe_path(data.curated_root(),item.relative_path)
        if not path.is_file(): return Response(status=404)
        response = FileResponse(path.open('rb'),content_type='image/jpeg')
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        return response


class LearningView(AdminView):
    def get(self,request):
        rows = data.current_rows()
        result = data.readiness(rows)
        result['storage'] = data.storage_usage()
        result['cameras'] = list(Camera.objects.filter(status=1).values('id','description'))
        result['examples'] = rows[:100]
        # Selected reviews and normal audits have distinct, explicitly biased scopes.
        result['review_summary'] = {}
        for kind in ('divergence','audit'):
            labels = ImageLabel.objects.filter(selection__episode__kind=kind).select_related('selection').order_by('-created_at')
            latest = {}
            for label in labels: latest.setdefault(label.selection_id,label)
            valid = [l for l in latest.values() if l.classification in data.CLASSES]
            result['review_summary'][kind] = {'count':len(valid),'agreement':sum(l.classification==l.selection.classification for l in valid)/len(valid) if valid else None,
                'scope':'Concordância nas imagens revisadas; não representa acurácia geral.'}
        return Response(result)


class DatasetsView(AdminView):
    def get(self,request):
        current = data.current_rows()
        return Response(paginate(LearningDataset.objects.order_by('-created_at'),request,lambda d:{'id':str(d.pk),'sha256':d.sha256,
            'created_at':d.created_at,'readiness':d.manifest['readiness'],'current':data.dataset_current(d,current)}))
    def post(self,request):
        dataset = data.create_dataset(request.user)
        return Response({'id':dataset.pk,'readiness':dataset.manifest['readiness']},status=201)


def job_payload(job):
    return {'id':str(job.pk),'dataset_id':str(job.dataset_id),'parent_id':str(job.parent_id),'candidate_id':str(job.candidate_id) if job.candidate_id else None,
        'status':job.status,'progress':job.progress,'error_code':job.error_code,'created_at':job.created_at,'updated_at':job.updated_at}


class JobsView(AdminView):
    def get(self,request): return Response(paginate(TrainingJob.objects.order_by('-created_at'),request,job_payload))
    def post(self,request):
        class Input(serializers.Serializer):
            dataset_id = serializers.UUIDField()
            request_id = serializers.UUIDField()
            epochs = serializers.IntegerField(min_value=1,max_value=10,default=3)
        form=Input(data=request.data); form.is_valid(raise_exception=True)
        dataset=get_object_or_404(LearningDataset,pk=form.validated_data['dataset_id'])
        job=training.request_training(dataset,request.user,form.validated_data['request_id'],form.validated_data['epochs'])
        return Response(job_payload(job),status=201)


class JobView(AdminView):
    def post(self,request,job_id):
        return Response(job_payload(training.control_job(job_id,request.data.get('action'))))


class VersionsView(AdminView):
    def get(self,request):
        current_rows = data.current_rows()
        def render(v):
            return {'id':str(v.pk),'name':v.name,'status':v.status,'sha256':v.sha256,'manifest':{k:val for k,val in v.manifest.items() if k not in ('training_event_ids','training_image_hashes')},
                'comparison':lifecycle.comparison(v,current_rows),'pilot_blockers':lifecycle.pilot_errors(v),
                'previously_operational':ModelDeployment.objects.filter(target=v,action__in=['BOOTSTRAP','PILOT','EXPAND','ROLLBACK']).exists(),
                'pilots':list(ModelAssignment.objects.filter(model=v).values('camera_id','started_at'))}
        result=paginate(FloodModelVersion.objects.order_by('-created_at'),request,render)
        result['deployments']=list(ModelDeployment.objects.order_by('-created_at').values('id','action','camera_id','previous_id','target_id','reason','created_at','actor__name')[:30])
        return Response(result)
    def post(self,request):
        form=ActionInput(data=request.data);form.is_valid(raise_exception=True)
        if form.validated_data['action']!='bootstrap': raise ValidationError('Ação inválida.')
        version=lifecycle.bootstrap(request.user,form.validated_data['request_id'])
        return Response({'id':version.pk},status=201)


class VersionActionView(AdminView):
    def post(self,request,version_id):
        form=ActionInput(data=request.data);form.is_valid(raise_exception=True)
        values=form.validated_data
        if values['action']=='evaluate':
            if not values.get('dataset_id'): raise ValidationError('Selecione um conjunto para a comparação.')
            job=training.request_evaluation(get_object_or_404(LearningDataset,pk=values['dataset_id']),
                get_object_or_404(FloodModelVersion,pk=version_id),request.user,values['request_id'])
            return Response(job_payload(job),status=201)
        if values['action']=='shadow':
            session=lifecycle.start_shadow(version_id,request.user,values['request_id'])
            return Response({'id':session.pk})
        deployment=lifecycle.deploy(version_id,request.user,values['action'],values['reason'],values['request_id'],values.get('camera_id'))
        return Response({'id':deployment.pk})
