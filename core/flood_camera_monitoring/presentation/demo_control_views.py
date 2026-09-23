"""Lightweight demo control views used even without the ML service."""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.flood_camera_monitoring.infra.demo_stream_client import (
    DemoStreamClient,
    DemoStreamUnavailable,
)
from core.flood_camera_monitoring.presentation.serializers import DemoStateSerializer
from core.users.permissions import IsAppAdmin


logger = logging.getLogger(__name__)


def _client() -> DemoStreamClient:
    return DemoStreamClient()


def _public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    output = dict(payload)
    output.pop("sources", None)
    source = output.get("source")
    if isinstance(source, dict):
        output["source"] = {
            key: value
            for key, value in source.items()
            if key in {"type", "mode", "status"}
        }
    segment = output.get("segment")
    if isinstance(segment, dict):
        output["segment"] = {
            key: value for key, value in segment.items() if key != "internal_url"
        }
    return output


class DemoStatusView(APIView):
    permission_classes = [permissions.AllowAny]
    require_enabled = False

    def get(self, request, *args, **kwargs):
        if self.require_enabled and not settings.DEMO_ENABLED:
            return Response({"enabled": False, "status": "disabled"})
        try:
            payload = _client().get_state()
        except DemoStreamUnavailable as exc:
            return Response(
                {
                    "enabled": bool(self.require_enabled),
                    "status": "unavailable",
                    "detail": str(exc),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"enabled": True, **_public_payload(payload)})


class DemoStateView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAppAdmin]
    require_enabled = False

    def post(self, request, *args, **kwargs):
        if self.require_enabled and not settings.DEMO_ENABLED:
            return Response(
                {"detail": "Demo stream is disabled"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        serializer = DemoStateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        requested_state = serializer.validated_data["state"]
        client = _client()
        try:
            previous = client.get_state()
            available_states = previous.get("available_states", [])
            if requested_state not in available_states:
                return Response(
                    {
                        "detail": f"State '{requested_state}' is not available in this scenario",
                        "available_states": available_states,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            payload = client.set_state(requested_state)
        except DemoStreamUnavailable as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        logger.info(
            "Demo state changed user_id=%s previous=%s current=%s session_id=%s",
            getattr(request.user, "id", None),
            previous.get("demo_state"),
            payload.get("demo_state"),
            payload.get("session_id"),
        )
        from core.uploader.models import DemoVideoSource

        DemoVideoSource.objects.exclude(mode=requested_state).update(active=False)
        DemoVideoSource.objects.filter(mode=requested_state).update(active=True)
        return Response({"enabled": True, **_public_payload(payload)})
