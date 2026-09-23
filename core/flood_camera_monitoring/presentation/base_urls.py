"""Flood URL subset that is safe in the lightweight development gateway."""

from django.urls import path

from core.flood_camera_monitoring.presentation.demo_control_views import (
    DemoStateView,
    DemoStatusView,
)
from core.flood_camera_monitoring.presentation.camera_views import CameraMetadataViewSet
from core.flood_camera_monitoring.presentation.demo_source_views import (
    DemoSourcesView,
    DemoSourceUploadView,
)


urlpatterns = [
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
    path("demo", DemoStatusView.as_view(), name="demo-status"),
    path("demo/state", DemoStateView.as_view(), name="demo-state"),
    path("demo/sources", DemoSourcesView.as_view(), name="demo-sources"),
    path(
        "demo/sources/<str:mode>",
        DemoSourceUploadView.as_view(),
        name="demo-source-upload",
    ),
]
