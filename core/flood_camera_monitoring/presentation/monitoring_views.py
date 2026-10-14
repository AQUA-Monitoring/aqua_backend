from __future__ import annotations

import uuid

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from config.pagination import DefaultPageNumberPagination
from core.common.mixins import SafeOrderingMixin
from core.flood_camera_monitoring.infra.models import Camera
from core.flood_camera_monitoring.presentation.camera_views import _snapshot_prediction_response
from core.flood_camera_monitoring.presentation.serializers import StreamBatchSerializer, StreamSnapshotSerializer
from core.flood_camera_monitoring.presentation.utils import build_prediction_payload

class FloodMonitoringViewSet(SafeOrderingMixin, viewsets.ViewSet):
    # Ordering config for cameras list
    ordering_map = {
        "description": "description",
        "created_at": "created_at",
        "updated_at": "updated_at",
        "status": "status",
        "neighborhood": "neighborhood__name",
        "region": "neighborhood__region__name",
        "latitude": "latitude",
        "longitude": "longitude",
    }
    default_ordering = "description"

    # Legacy POST handler removed; use explicit actions below

    @action(detail=False, methods=["get"], url_path="predict/all")
    def predict_all(self, request):
        return _snapshot_prediction_response(request, self)

    @action(detail=False, methods=["post"], url_path="predict/snapshot")
    def predict_snapshot(self, request):
        from core.flood_camera_monitoring.infra.opencv_stream import (
            OpenCVVideoStream,
        )
        from core.flood_camera_monitoring.services.stream_prediction import (
            predict_snapshot,
        )
        from core.flood_camera_monitoring.infra.torch_flood_classifier import (
            build_default_classifier,
        )

        serializer = StreamSnapshotSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        from core.flood_camera_monitoring.services.model_lifecycle import operational_version, compatible
        camera = Camera.objects.filter(video_hls=data['stream_url']).first()
        version = operational_version(camera.pk if camera else None)
        clf = compatible(version) if version else build_default_classifier()
        stream = OpenCVVideoStream(data["stream_url"])  # implements VideoStreamPort
        try:
            res = predict_snapshot(
                classifier=clf,
                stream=stream,
                timeout_seconds=float(data.get("timeout_seconds", 5.0)),
            )
        except TimeoutError:
            return Response(
                {"detail": "Could not capture frame"},
                status=status.HTTP_504_GATEWAY_TIMEOUT,
            )

        payload = build_prediction_payload(res)
        payload['model_version'] = version.sha256 if version else None
        return Response(payload)

    @action(detail=False, methods=["post"], url_path="predict/batch")
    def predict_batch(self, request):
        from core.flood_camera_monitoring.infra.opencv_stream import (
            OpenCVVideoStream,
        )
        from core.flood_camera_monitoring.services.stream_prediction import (
            predict_stream,
        )
        from core.flood_camera_monitoring.infra.torch_flood_classifier import (
            build_default_classifier,
        )

        serializer = StreamBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        from core.flood_camera_monitoring.services.model_lifecycle import operational_version, compatible
        camera = Camera.objects.filter(video_hls=data['stream_url']).first()
        version = operational_version(camera.pk if camera else None)
        clf = compatible(version) if version else build_default_classifier()
        stream = OpenCVVideoStream(data["stream_url"])  # VideoStreamPort
        results = [
            build_prediction_payload(res)
            for res in predict_stream(
                classifier=clf,
                stream=stream,
                interval_seconds=float(data.get("interval_seconds", 2.0)),
                max_iterations=int(data.get("max_iterations", 3)),
            )
        ]

        return Response({"results": results, "model_version": version.sha256 if version else None})

    @action(detail=False, methods=["post"], url_path="analyze/all")
    def analyze_all(self, request):
        from core.flood_camera_monitoring.services.analyze import (
            AnalyzeAllCamerasService,
        )

        service = AnalyzeAllCamerasService()
        saved = service.run()
        return Response({"saved": saved})

    @action(detail=False, methods=["get"], url_path="cameras")
    def cameras(self, request):
        neighborhood_id = request.query_params.get("neighborhood_id")
        region_id = request.query_params.get("region_id")
        neighborhood_name = request.query_params.get("neighborhood")
        ordering_param = request.query_params.get("ordering", "description")

        qs = Camera.objects.select_related("neighborhood", "neighborhood__region").all()

        if neighborhood_id:
            try:
                nb_uuid = uuid.UUID(str(neighborhood_id))
                qs = qs.filter(neighborhood_id=nb_uuid)
            except (ValueError, AttributeError):
                return Response(
                    {"detail": "neighborhood_id inválido"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if region_id:
            try:
                rg_uuid = uuid.UUID(str(region_id))
                qs = qs.filter(neighborhood__region_id=rg_uuid)
            except (ValueError, AttributeError):
                return Response(
                    {"detail": "region_id inválido"}, status=status.HTTP_400_BAD_REQUEST
                )

        if neighborhood_name:
            qs = qs.filter(neighborhood__name__icontains=neighborhood_name)

        qs = self.apply_ordering(qs, ordering_param)

        paginator = DefaultPageNumberPagination()
        page_items = paginator.paginate_queryset(qs, request, view=self)

        data_out = []
        for cam in page_items:
            nb = cam.neighborhood
            rg = nb.region if nb else None
            data_out.append(
                {
                    "id": str(cam.id),
                    "description": cam.description,
                    "status": cam.get_status_display(),
                    "video_hls": cam.video_hls,
                    "video_embed": cam.video_embed,
                    "neighborhood": (
                        {"id": str(nb.id), "name": nb.name} if nb else None
                    ),
                    "region": ({"id": str(rg.id), "name": rg.name} if rg else None),
                    "latitude": cam.latitude,
                    "longitude": cam.longitude,
                }
            )

        # get_paginated_response will include count/next/previous and ordering
        return paginator.get_paginated_response(data_out)


## Removed duplicate StreamBatchDetectView; use ViewSet action predict/batch
