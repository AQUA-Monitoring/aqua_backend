"""Full Flood Monitoring API; imported only by flood/full images."""

from django.urls import path
from .adaptive_views import MonitoringView, EvidenceView, ReviewView, ApproveReviewView, ModelsView, PromoteModelView, MonitoringHealthView

from core.flood_camera_monitoring.presentation.demo_views import (
    DemoPredictionBatchView,
    DemoPredictionFrameView,
    DemoPredictView,
    DemoStateView,
    DemoStatusView,
)
from core.flood_camera_monitoring.presentation.camera_views import CameraMetadataViewSet
from core.flood_camera_monitoring.presentation.demo_source_views import (
    DemoSourcesView,
    DemoSourceUploadView,
)
from core.flood_camera_monitoring.presentation.health_views import HealthcheckView
from core.flood_camera_monitoring.presentation.monitoring_views import FloodMonitoringViewSet


urlpatterns = [
    path('monitoring/health/', MonitoringHealthView.as_view()),
    path('reviews/<uuid:review_id>/approve/', ApproveReviewView.as_view()),
    path('models/', ModelsView.as_view()),
    path('models/<uuid:model_id>/promote/', PromoteModelView.as_view()),
    path('cameras/<uuid:camera_id>/monitoring/', MonitoringView.as_view()),
    path('evidence/<uuid:evidence_id>/', EvidenceView.as_view()),
    path('analyses/<uuid:run_id>/reviews/', ReviewView.as_view()),
    path(
        "stream/snapshot",
        FloodMonitoringViewSet.as_view({"post": "predict_snapshot"}),
        name="stream-snapshot-detect",
    ),
    path(
        "predict/all/",
        CameraMetadataViewSet.as_view({"get": "predict_all"}),
        name="predict-all-cameras",
    ),
    path(
        "cameras/",
        CameraMetadataViewSet.as_view({"get": "list", "post": "create"}),
        name="cameras-list",
    ),
    path(
        "cameras/<uuid:pk>/",
        CameraMetadataViewSet.as_view({"get": "retrieve", "put": "update", "patch": "partial_update"}),
        name="cameras-detail",
    ),
    path(
        "cameras/<uuid:pk>/nearby/",
        CameraMetadataViewSet.as_view({"get": "nearby"}),
        name="cameras-nearby",
    ),
    path("health/", HealthcheckView.as_view(), name="flood-health"),
    path("demo", DemoStatusView.as_view(), name="demo-status"),
    path("demo/state", DemoStateView.as_view(), name="demo-state"),
    path("demo/predict", DemoPredictView.as_view(), name="demo-predict"),
    path(
        "demo/predictions/batch",
        DemoPredictionBatchView.as_view(),
        name="demo-predictions-batch",
    ),
    path(
        "demo/predictions/frames/<str:frame_id>",
        DemoPredictionFrameView.as_view(),
        name="demo-prediction-frame",
    ),
    path("demo/sources", DemoSourcesView.as_view(), name="demo-sources"),
    path(
        "demo/sources/<str:mode>",
        DemoSourceUploadView.as_view(),
        name="demo-source-upload",
    ),
]
