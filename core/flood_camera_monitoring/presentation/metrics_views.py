"""Private Prometheus endpoint for persisted camera and ML state."""

from __future__ import annotations

import secrets

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from core.flood_camera_monitoring.services.metrics import (
    render_camera_ml_metrics,
    render_collection_failure_metrics,
)


PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _configured_token() -> str:
    return str(getattr(settings, "INTERNAL_METRICS_TOKEN", "")).strip()


def _authorized(request: HttpRequest, expected_token: str) -> bool:
    prefix = "Bearer "
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith(prefix):
        return False
    provided_token = authorization[len(prefix) :]
    return bool(provided_token) and secrets.compare_digest(
        provided_token,
        expected_token,
    )


def _metrics_response(payload: bytes, *, status: int = 200) -> HttpResponse:
    response = HttpResponse(
        payload,
        status=status,
        content_type=PROMETHEUS_CONTENT_TYPE,
    )
    response["Cache-Control"] = "no-store"
    return response


@never_cache
@require_GET
def internal_metrics(request: HttpRequest) -> HttpResponse:
    token = _configured_token()
    if not getattr(settings, "INTERNAL_METRICS_ENABLED", False) or not token:
        return HttpResponse(status=404)
    if not _authorized(request, token):
        return HttpResponse(status=403)

    try:
        return _metrics_response(render_camera_ml_metrics())
    except Exception:
        # Scrapers receive an explicit failure signal without exception details.
        return _metrics_response(render_collection_failure_metrics(), status=503)
