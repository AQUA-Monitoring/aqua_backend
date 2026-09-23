from datetime import timedelta

from django.core import signing
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db import transaction
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from core.users.permissions import IsAppAdmin
from core.flood_camera_monitoring.infra.models import (
    Camera, CameraMonitoringState, CameraAnalysisRun, CameraEvidence, CameraReview, MonitoringAudit, FloodModelVersion,
)
from core.flood_camera_monitoring.services.adaptive_monitoring import evidence_root


class IntensityInput(serializers.Serializer):
    level = serializers.ChoiceField(choices=['NORMAL', 'WATCH', 'CRITICAL', 'RECOVERY'])
    reason = serializers.CharField(max_length=500, allow_blank=False)
    minutes = serializers.IntegerField(min_value=1, max_value=1440, default=60)


class ReviewInput(serializers.Serializer):
    classification = serializers.ChoiceField(choices=['NO_INDICATION', 'INTERMEDIATE_INDICATION', 'FLOOD_INDICATION', 'UNUSABLE'])
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    event_id = serializers.CharField(max_length=100, required=False, default='')


class MonitoringView(APIView):
    permission_classes = [IsAppAdmin]

    def get(self, request, camera_id):
        get_object_or_404(Camera, pk=camera_id)
        state = CameraMonitoringState.objects.filter(pk=camera_id).values(
            'level', 'reason', 'next_analysis_at', 'strong_streak', 'clear_streak', 'lease_until').first()
        runs = []
        for run in CameraAnalysisRun.objects.filter(camera_id=camera_id).prefetch_related('evidence', 'reviews').order_by('-started_at')[:50]:
            evidence = []
            for item in run.evidence.all():
                if item.deleted_at or item.expires_at <= timezone.now():
                    continue
                token = signing.dumps({'id': str(item.pk), 'user': str(request.user.pk)}, salt='flood-evidence')
                evidence.append({'id': item.pk, 'sha256': item.sha256, 'captured_at': item.captured_at,
                    'url': f'/api/flood_monitoring/evidence/{item.pk}/?token={token}'})
            runs.append({'id': run.pk, 'started_at': run.started_at, 'finished_at': run.finished_at,
                'level': run.level, 'result': run.result, 'frames': run.frames,
                'context': run.context, 'model_version': run.model_version, 'latency_ms': run.latency_ms,
                'error_code': run.error_code, 'evidence': evidence,
                'reviews': list(run.reviews.values('id', 'classification', 'reason', 'approved', 'created_at'))})
        return Response({'monitoring': state, 'runs': runs})

    def post(self, request, camera_id):
        camera = get_object_or_404(Camera, pk=camera_id, status=1)
        data = IntensityInput(data=request.data)
        data.is_valid(raise_exception=True)
        with transaction.atomic():
            state, _ = CameraMonitoringState.objects.get_or_create(camera=camera)
            state = CameraMonitoringState.objects.select_for_update().get(pk=camera_id)
            state.level = data.validated_data['level']
            state.reason = 'MANUAL'
            state.manual_until = timezone.now() + timedelta(minutes=data.validated_data['minutes'])
            state.next_analysis_at = timezone.now()
            state.save()
            MonitoringAudit.objects.create(camera=camera, actor=request.user, action='MANUAL_INTENSITY', details=data.validated_data)
        return Response({'level': state.level, 'until': state.manual_until})


class EvidenceView(APIView):
    permission_classes = [IsAppAdmin]

    def get(self, request, evidence_id):
        try:
            payload = signing.loads(request.query_params.get('token', ''), salt='flood-evidence', max_age=300)
        except signing.BadSignature:
            return Response({'detail': 'Link expirado ou inválido'}, status=403)
        if payload != {'id': str(evidence_id), 'user': str(request.user.pk)}:
            return Response(status=403)
        item = get_object_or_404(CameraEvidence, pk=evidence_id, deleted_at__isnull=True, expires_at__gt=timezone.now())
        path = (evidence_root() / item.relative_path).resolve()
        if not path.is_relative_to(evidence_root()) or not path.is_file():
            return Response(status=404)
        response = FileResponse(path.open('rb'), content_type='image/jpeg')
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        return response


class ReviewView(APIView):
    permission_classes = [IsAppAdmin]

    def post(self, request, run_id):
        run = get_object_or_404(CameraAnalysisRun, pk=run_id)
        data = ReviewInput(data=request.data)
        data.is_valid(raise_exception=True)
        review = CameraReview.objects.create(run=run, actor=request.user, **data.validated_data)
        return Response({'id': review.pk, 'approved': False}, status=201)


class ApproveReviewView(APIView):
    permission_classes = [IsAppAdmin]

    def post(self, request, review_id):
        original = get_object_or_404(CameraReview, pk=review_id, approved=False)
        if original.actor_id == request.user.pk:
            return Response({'detail': 'A revisão deve ser validada por outro operador.'}, status=400)
        data = ReviewInput(data=request.data)
        data.is_valid(raise_exception=True)
        if not data.validated_data['event_id']:
            return Response({'detail': 'Identifique o evento para separar treino e teste.'}, status=400)
        review = CameraReview.objects.create(run=original.run, actor=request.user,
            approved=True, approval_of=original, **data.validated_data)
        return Response({'id': review.pk}, status=201)


class ModelsView(APIView):
    permission_classes = [IsAppAdmin]

    def get(self, request):
        return Response(list(FloodModelVersion.objects.order_by('-created_at').values(
            'id', 'name', 'provider', 'sha256', 'status', 'metrics', 'manifest')[:50]))


class PromoteModelView(APIView):
    permission_classes = [IsAppAdmin]

    def post(self, request, model_id):
        from django.core.exceptions import ValidationError
        from core.flood_camera_monitoring.services.model_registry import promote_model
        get_object_or_404(FloodModelVersion, pk=model_id)
        try:
            version = promote_model(model_id, request.user)
        except ValidationError as exc:
            return Response({'detail': exc.messages}, status=400)
        return Response({'id': version.pk, 'status': version.status})


class MonitoringHealthView(APIView):
    permission_classes = [IsAppAdmin]

    def get(self, request):
        from django.db.models import Count, Avg, Q
        now = timezone.now()
        recent = CameraAnalysisRun.objects.filter(finished_at__gte=now-timedelta(hours=1))
        from core.flood_camera_monitoring.services.drift import distribution_shift
        def probabilities(query):
            return [sum(frame['flooded'] for frame in frames)/len(frames)
                for frames in query.values_list('frames', flat=True)[:10000] if frames]
        drift = distribution_shift(probabilities(recent), probabilities(CameraAnalysisRun.objects.filter(
            finished_at__gte=now-timedelta(hours=25), finished_at__lt=now-timedelta(hours=1))))
        stale = Camera.objects.filter(status=1).filter(
            Q(operational_snapshot__analyzed_at__lt=now-timedelta(minutes=10)) |
            Q(operational_snapshot__analyzed_at__isnull=True))
        return Response({'at': now,
            'active_cameras': Camera.objects.filter(status=1).count(),
            'technical_alerts': list(stale.values('id', 'description', 'operational_snapshot__analysis_status')),
            'due_cameras': CameraMonitoringState.objects.filter(next_analysis_at__lte=now, camera__status=1).count(),
            'leased_cameras': CameraMonitoringState.objects.filter(lease_until__gt=now).count(),
            'levels': list(CameraMonitoringState.objects.values('level').annotate(count=Count('camera'))),
            'last_hour': recent.aggregate(runs=Count('id'), mean_latency_ms=Avg('latency_ms')),
            'failed_runs': recent.exclude(error_code='').count(),
            'distribution_shift': drift,
            'runs_without_evidence': recent.filter(evidence__isnull=True).count(),
            'pending_reviews': CameraReview.objects.filter(approved=False).count()})
