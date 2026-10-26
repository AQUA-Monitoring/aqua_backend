from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status
from core.users.password_confirmation import AdminPasswordThrottle, require_admin_password
from core.flood_point_registering.services import sync_legacy_spatial_event
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet
from rest_framework.permissions import SAFE_METHODS, AllowAny

from core.users.permissions import IsAppAdmin
from core.flood_point_registering.infra.models import Flood_Point_Register
from core.flood_point_registering.presentation.serializers.RegisterSerializer import (
    FloodPointRegisterSerializer,
)


class ChangeOptionsSerializer(serializers.Serializer):
    notify = serializers.BooleanField(default=False)


class FloodPointRegister(ModelViewSet):
    queryset = Flood_Point_Register.objects.select_related("spatial_event__current_revision")
    serializer_class = FloodPointRegisterSerializer

    def get_throttles(self):
        if self.action in {"update", "partial_update", "destroy"}:
            return [AdminPasswordThrottle()]
        return super().get_throttles()

    def get_permissions(self):
        if self.request.method in SAFE_METHODS:
            return [AllowAny()]
        return [IsAppAdmin()]

    def change_options(self, request):
        require_admin_password(request)
        options = ChangeOptionsSerializer(data=request.data)
        options.is_valid(raise_exception=True)
        notify = options.validated_data["notify"]
        if notify and not settings.UNIFIED_NOTIFICATIONS_ENABLED:
            raise serializers.ValidationError({"notify": "As notificações estão indisponíveis no momento."})
        return notify

    def publish_change(self, point, notify, change):
        if notify:
            from core.notifications.services import create_flood_point_event, publish_event
            event = create_flood_point_event(point, actor=self.request.user, change=change)
            publish_event(event, actor=self.request.user)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        notify = self.change_options(request)
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        instance = Flood_Point_Register.objects.select_for_update().get(pk=instance.pk)
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        self.publish_change(serializer.instance, notify, "updated")
        return Response(serializer.data)

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        notify = self.change_options(request)
        instance = self.get_object()
        instance = Flood_Point_Register.objects.select_for_update().get(pk=instance.pk)
        if instance.finished_at <= timezone.now():
            return Response(status=status.HTTP_204_NO_CONTENT)
        # Encerramento lógico: preserva referências de notificações e revisões.
        instance.finished_at = timezone.now()
        instance.save(update_fields=["finished_at"])
        sync_legacy_spatial_event(instance, author=request.user)
        self.publish_change(instance, notify, "ended")
        return Response(status=status.HTTP_204_NO_CONTENT)

    def get_queryset(self):
        qs = Flood_Point_Register.objects.select_related("spatial_event__current_revision")
        # Only return ACTIVE points by default in list and custom active action
        if getattr(self, "action", None) in {"list", "active"}:
            return qs.active().order_by("-created_at")
        return qs

    @action(detail=False, methods=["get"], url_path="active")
    def active(self, request):
        # Active = created_at <= now <= finished_at
        qs = self.get_queryset().order_by("-created_at")
        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)
