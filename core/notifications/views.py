from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.users.permissions import IsAppAdmin

from .models import NotificationEvent, NotificationEventAudit, PushSubscription, RegionSubscription, SavedPlace
from .serializers import (
    PushSubscriptionDeleteSerializer,
    PushSubscriptionSerializer,
    RegionSubscriptionDeleteSerializer,
    RegionSubscriptionSerializer,
    OperationalAlertFilterSerializer,
    ReasonSerializer,
    ResolveSerializer,
    NotificationEventSerializer,
    SavedPlaceSerializer,
)
from .services import preview_event_audience, publish_event, schedule_publication_push
from core.flood_camera_monitoring.services.operational_alerts import canonical_region_for_camera


class RegionSubscriptionViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def list(self, request):
        queryset = RegionSubscription.objects.filter(user=request.user).select_related(
            "region__city_ref"
        )
        from config.pagination import DefaultPageNumberPagination

        paginator = DefaultPageNumberPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(RegionSubscriptionSerializer(page, many=True).data)

    def create(self, request):
        serializer = RegionSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = {key: serializer.validated_data[key] for key in ("region", "neighborhood") if serializer.validated_data.get(key)}
        subscription, created = RegionSubscription.objects.get_or_create(user=request.user, **values)
        output = RegionSubscriptionSerializer(subscription)
        return Response(
            output.data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def destroy_collection(self, request):
        serializer = RegionSubscriptionDeleteSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        values = {key: serializer.validated_data[key] for key in ("region", "neighborhood") if serializer.validated_data.get(key)}
        RegionSubscription.objects.filter(user=request.user, **values).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SavedPlaceViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = SavedPlaceSerializer

    def get_queryset(self):
        return SavedPlace.objects.filter(user=self.request.user).select_related("city", "region", "neighborhood")

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class NotificationEventViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsAppAdmin]
    serializer_class = NotificationEventSerializer

    def get_queryset(self):
        queryset = NotificationEvent.objects.select_related("actor").prefetch_related("regions__city_ref", "neighborhoods", "push_deliveries")
        for field in ("origin", "status", "severity"):
            if self.request.query_params.get(field):
                queryset = queryset.filter(**{field: self.request.query_params[field]})
        if self.request.query_params.get("date_from"):
            queryset = queryset.filter(created_at__gte=self.request.query_params["date_from"])
        if self.request.query_params.get("date_to"):
            queryset = queryset.filter(created_at__lte=self.request.query_params["date_to"])
        return queryset

    def destroy(self, request, *args, **kwargs):
        event = self.get_object()
        if event.status != NotificationEvent.Status.DRAFT:
            return Response({"detail": "Somente rascunhos podem ser excluídos."}, status=status.HTTP_409_CONFLICT)
        return super().destroy(request, *args, **kwargs)

    def perform_update(self, serializer):
        if serializer.instance.status != NotificationEvent.Status.DRAFT:
            from rest_framework.exceptions import ValidationError
            raise ValidationError("Somente rascunhos podem ser alterados.")
        serializer.save()

    @action(detail=True, methods=["get"])
    def preview(self, request, pk=None):
        return Response(preview_event_audience(self.get_object()))

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        try:
            event = publish_event(self.get_object(), actor=request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(self.get_serializer(event).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        event = self.get_object()
        if event.status != NotificationEvent.Status.DRAFT:
            return Response({"detail": "Somente rascunhos podem ser cancelados."}, status=status.HTTP_409_CONFLICT)
        event.status = NotificationEvent.Status.CANCELED
        event.save(update_fields=["status", "updated_at"])
        NotificationEventAudit.objects.create(event=event, action="CANCELED", actor=request.user)
        return Response(self.get_serializer(event).data)


class PushSubscriptionViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def create(self, request):
        serializer = PushSubscriptionSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        existed = PushSubscription.objects.filter(
            endpoint=serializer.validated_data["endpoint"]
        ).exists()
        subscription = serializer.save()
        return Response(
            PushSubscriptionSerializer(subscription).data,
            status=status.HTTP_200_OK if existed else status.HTTP_201_CREATED,
        )

    def destroy_collection(self, request):
        serializer = PushSubscriptionDeleteSerializer(
            data=request.query_params if request.query_params.get("endpoint") else request.data
        )
        serializer.is_valid(raise_exception=True)
        PushSubscription.objects.filter(
            user=request.user,
            endpoint=serializer.validated_data["endpoint"],
        ).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"], url_path="config")
    def config(self, request):
        return Response(
            {
                "enabled": settings.WEB_PUSH_ENABLED,
                "public_key": settings.WEB_PUSH_VAPID_PUBLIC_KEY if settings.WEB_PUSH_ENABLED else "",
                "application_server_key": settings.WEB_PUSH_VAPID_PUBLIC_KEY if settings.WEB_PUSH_ENABLED else "",
            }
        )


class OperationalAlertViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, IsAppAdmin]

    @staticmethod
    def _queryset():
        from core.flood_camera_monitoring.infra.models import OperationalAlert

        return OperationalAlert.objects.select_related(
            "camera",
            "region__city_ref",
            "confirmed_by",
            "initial_detection",
            "latest_detection",
        ).prefetch_related("transitions__actor")

    @staticmethod
    def _serialize(alert):
        def serialize_detection(detection):
            image_url = None
            if detection.image:
                try:
                    image_url = detection.image.url
                except ValueError:
                    image_url = None
            return {
                "id": str(detection.id),
                "camera_id": str(detection.camera_id),
                "created_at": detection.created_at,
                "is_flooded": detection.is_flooded,
                "medium": detection.medium,
                "confidence": detection.confidence,
                "probabilities": {
                    "normal": detection.prob_normal,
                    "medium": detection.prob_medium,
                    "flooded": detection.prob_flooded,
                },
                "image_url": image_url,
            }

        transitions = [
            {
                "id": str(item.id),
                "from_status": item.from_status,
                "to_status": item.to_status,
                "origin": item.origin,
                "source": (
                    "MODEL"
                    if item.origin == item.Origin.CAMERA_ANALYSIS
                    else item.origin
                ),
                "reason": item.reason,
                "metadata": item.metadata,
                "notify_subscribers": item.metadata.get("notify_subscribers"),
                "actor": (
                    {
                        "id": str(item.actor_id),
                        "name": item.actor.name,
                        "email": item.actor.email,
                    }
                    if item.actor_id
                    else None
                ),
                "created_at": item.created_at,
            }
            for item in alert.transitions.all()
        ]
        try:
            publication = alert.publication
        except ObjectDoesNotExist:
            publication = None
        delivery_counts = {
            item["status"]: item["total"]
            for item in alert.push_deliveries.values("status").annotate(total=Count("id"))
        }
        delivery_summary = {
            "pending": delivery_counts.get("pending", 0),
            "sent": delivery_counts.get("sent", 0),
            "failed": delivery_counts.get("failed", 0),
            "expired": delivery_counts.get("expired", 0),
        }
        evidence = dict(alert.evidence or {})
        image_url = None
        if alert.latest_detection.image:
            try:
                image_url = alert.latest_detection.image.url
            except ValueError:
                image_url = None
        evidence.update(
            {
                "initial_detection_id": str(alert.initial_detection_id),
                "latest_detection_id": str(alert.latest_detection_id),
                "image_url": image_url,
            }
        )
        effective_region = canonical_region_for_camera(alert.camera) or alert.region
        return {
            "id": str(alert.id),
            "status": alert.status,
            "camera": {
                "id": str(alert.camera_id),
                "description": alert.camera.description,
                "administrative_status": alert.camera.get_status_display(),
                "detail_path": f"/cameras/{alert.camera_id}",
            },
            "region": (
                {
                    "id": str(effective_region.id),
                    "name": effective_region.name,
                    "city": {
                        "id": str(effective_region.city_ref_id) if effective_region.city_ref_id else "",
                        "name": effective_region.city,
                    },
                }
                if effective_region
                else None
            ),
            "evidence": evidence,
            "detection_records": {
                "initial": serialize_detection(alert.initial_detection),
                "latest": serialize_detection(alert.latest_detection),
            },
            "first_detected_at": alert.first_detected_at,
            "last_detected_at": alert.last_detected_at,
            "confirmed_at": alert.confirmed_at,
            "dismissed_at": alert.dismissed_at,
            "resolved_at": alert.resolved_at,
            "confirmed_by": (
                {"id": str(alert.confirmed_by_id), "name": alert.confirmed_by.name}
                if alert.confirmed_by_id
                else None
            ),
            "publication": (
                {
                    "id": str(publication.id),
                    "title": publication.title,
                    "message": publication.message,
                    "classification": publication.classification,
                    "confidence": publication.confidence,
                    "probabilities": publication.probabilities,
                    "model_version": publication.model_version,
                    "detected_at": publication.detected_at,
                    "confirmed_at": publication.confirmed_at,
                }
                if publication
                else None
            ),
            "transitions": transitions,
            "delivery_summary": delivery_summary,
            "deliveries": delivery_summary,
        }

    def list(self, request):
        filters = OperationalAlertFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        queryset = self._queryset()
        values = filters.validated_data
        if values.get("status"):
            queryset = queryset.filter(status=values["status"])
        if values.get("region"):
            queryset = queryset.filter(region_id=values["region"])
        if values.get("neighborhood_id"):
            neighborhood_id = values["neighborhood_id"]
            queryset = queryset.filter(
                Q(camera__address__neighborhood_id=neighborhood_id)
                | Q(
                    camera__address__isnull=True,
                    camera__neighborhood_id=neighborhood_id,
                )
            )
        if values.get("camera"):
            queryset = queryset.filter(camera_id=values["camera"])
        if values.get("date_from"):
            queryset = queryset.filter(last_detected_at__gte=values["date_from"])
        if values.get("date_to"):
            queryset = queryset.filter(last_detected_at__lte=values["date_to"])
        if values.get("ordering"):
            queryset = queryset.order_by(values["ordering"])
        from config.pagination import DefaultPageNumberPagination

        paginator = DefaultPageNumberPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response([self._serialize(item) for item in page])

    def _transition(self, request, pk, operation):
        from core.flood_camera_monitoring.infra.models import OperationalAlert
        from core.flood_camera_monitoring.services.operational_alerts import (
            AlertRegionRequired,
            InvalidAlertTransition,
            confirm_operational_alert,
            dismiss_operational_alert,
            resolve_operational_alert,
        )

        if not OperationalAlert.objects.filter(pk=pk).exists():
            return Response(
                {"code": "not_found", "detail": "Alerta operacional não encontrado."},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer_class = ResolveSerializer if operation == "resolve" else ReasonSerializer
        serializer = serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            if operation == "confirm":
                result = confirm_operational_alert(
                    pk,
                    request.user,
                    reason=serializer.validated_data.get("reason", ""),
                    on_published=schedule_publication_push,
                )
                alert = result.alert
            elif operation == "dismiss":
                alert = dismiss_operational_alert(
                    pk, request.user, reason=serializer.validated_data.get("reason", "")
                )
            else:
                alert = resolve_operational_alert(
                    pk,
                    request.user,
                    reason=serializer.validated_data.get("reason", ""),
                    notify_subscribers=serializer.validated_data["notify_subscribers"],
                    on_published=schedule_publication_push,
                )
        except InvalidAlertTransition as exc:
            return Response({"code": "invalid_transition", "detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except AlertRegionRequired as exc:
            return Response({"code": "region_unavailable", "detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        alert = self._queryset().get(pk=alert.pk)
        return Response(self._serialize(alert))

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        return self._transition(request, pk, "confirm")

    @action(detail=True, methods=["post"])
    def dismiss(self, request, pk=None):
        return self._transition(request, pk, "dismiss")

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        return self._transition(request, pk, "resolve")
