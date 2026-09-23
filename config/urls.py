"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path, include
from django.conf import settings

from rest_framework.routers import DefaultRouter
from core.users.presentation.auth_views import (
    AppTokenRefreshView,
    EmailTokenObtainPairView,
    SyncTokenView,
)
from core.uploader.router import router as uploader_router
from core.sync.export import ExportView
from config.core_health import health
from config.media import serve_media
from core.flood_camera_monitoring.presentation.metrics_views import internal_metrics

router = DefaultRouter()

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health, name="service-health"),
    path("internal/metrics", internal_metrics, name="internal-metrics"),
    # JWT auth endpoints
    # Single auth token route using email/password
    path(
        "api/auth/token/", EmailTokenObtainPairView.as_view(), name="token_obtain_pair"
    ),
    path(
        "api/auth/token/refresh/", AppTokenRefreshView.as_view(), name="token_refresh"
    ),
    path("api/sync/token/", SyncTokenView.as_view(), name="sync-token"),
    path("api/users/", include("core.users.presentation.urls")),
    path("api/weather/", include("core.weather.presentation.urls")),
    path("api/forecast/", include("core.forecast.presentation.urls")),
    path("api/occurrences/", include("core.occurrences.presentation.urls")),
    path("api/upload/", include(uploader_router.urls)),
    path("api/addressing/", include("core.addressing.presentation.urls")),
    path("api/flood-impact/", include("core.flood_impact.urls")),
    path("api/donate/", include("core.donate.presentation.urls")),
    path(
        "api/floods_point/", include("core.flood_point_registering.presentation.urls")
    ),
    path("api/blog/", include("core.blog.presentation.urls")),
    path("api/", include("core.notifications.urls")),
    path("api/export/", ExportView.as_view(), name="export-data"),
    path("media/<path:path>", serve_media, name="media-file"),
]

if settings.FLOOD_CAMERA_API_MODE == "proxy":
    urlpatterns.append(
        path(
            "api/flood_monitoring/",
            include("core.flood_camera_monitoring.presentation.base_urls"),
        )
    )
    urlpatterns.append(
        path(
            "api/flood_monitoring/",
            include("core.flood_camera_monitoring.presentation.proxy_urls"),
        )
    )
else:
    urlpatterns.append(
        path(
            "api/flood_monitoring/",
            include("core.flood_camera_monitoring.presentation.flood_urls"),
        )
    )
