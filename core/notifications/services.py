from __future__ import annotations

import logging
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .adapters import PushDeliveryError, WebPushAdapter
from .models import (
    NotificationEvent,
    NotificationEventAudit,
    PushDelivery,
    PushSubscription,
    RegionSubscription,
    SavedPlace,
)

logger = logging.getLogger(__name__)


def _alert_region_id(alert):
    return getattr(alert, "region_id", None)


def fanout_alert(alert, kind: str) -> int:
    """Persist one delivery per active device subscribed to the alert region."""
    region_id = _alert_region_id(alert)
    if not region_id:
        return 0
    subscriptions = PushSubscription.objects.filter(
        is_active=True,
        user__region_subscriptions__region_id=region_id,
    ).distinct()
    pending_ids = []
    for subscription in subscriptions:
        delivery, created = PushDelivery.objects.get_or_create(
            operational_alert=alert,
            subscription=subscription,
            kind=kind,
        )
        if created:
            pending_ids.append(delivery.id)
    if pending_ids:
        from .tasks import deliver_push_batch_task

        transaction.on_commit(lambda: deliver_push_batch_task.delay([str(i) for i in pending_ids]))
    return len(pending_ids)


def schedule_confirmed_alert(alert) -> int:
    return fanout_alert(alert, PushDelivery.Kind.CONFIRMED)


def schedule_resolved_alert(alert) -> int:
    return fanout_alert(alert, PushDelivery.Kind.RESOLVED)


def schedule_publication_push(publication, event_kind: str) -> int:
    if not settings.UNIFIED_NOTIFICATIONS_ENABLED:
        kind = PushDelivery.Kind.CONFIRMED if event_kind == "CONFIRMED" else PushDelivery.Kind.RESOLVED
        return fanout_alert(publication.alert, kind)
    event = create_camera_event(publication, event_kind)
    event = publish_event(event, actor=getattr(publication, "confirmed_by", None))
    return event.audience_count


def _snapshot(event: NotificationEvent) -> dict:
    return {
        "regions": [
            {"id": str(item.id), "name": item.name} for item in event.regions.all()
        ],
        "neighborhoods": [
            {"id": str(item.id), "name": item.name}
            for item in event.neighborhoods.all()
        ],
    }


def _nearby_user_ids(event: NotificationEvent) -> set:
    if not event.geometry:
        return set()
    event_geometry = event.geometry.clone()
    event_geometry.transform(3857)
    users = set()
    for place in SavedPlace.objects.only("user_id", "location", "radius_km"):
        location = place.location.clone()
        location.transform(3857)
        if event_geometry.distance(location) <= place.radius_km * 1000:
            users.add(place.user_id)
    return users


def event_recipient_user_ids(event: NotificationEvent) -> set:
    if event.origin == NotificationEvent.Origin.CAMERA and event.idempotency_key.endswith(":resolved"):
        original = NotificationEvent.objects.filter(
            idempotency_key=event.idempotency_key.removesuffix(":resolved") + ":confirmed"
        ).first()
        if original:
            return set(original.push_deliveries.values_list("subscription__user_id", flat=True))
    region_ids = list(event.regions.values_list("id", flat=True))
    neighborhood_ids = list(event.neighborhoods.values_list("id", flat=True))
    territory_users = RegionSubscription.objects.filter(
        Q(region_id__in=region_ids) | Q(neighborhood_id__in=neighborhood_ids)
    ).values_list("user_id", flat=True)
    return set(territory_users) | _nearby_user_ids(event)


def preview_event_audience(event: NotificationEvent) -> dict:
    users = event_recipient_user_ids(event)
    devices = PushSubscription.objects.filter(user_id__in=users, is_active=True).count()
    return {"users": len(users), "devices": devices}


def fanout_event(event: NotificationEvent, kind=PushDelivery.Kind.CONFIRMED) -> int:
    user_ids = event_recipient_user_ids(event)
    subscriptions = PushSubscription.objects.filter(user_id__in=user_ids, is_active=True)
    pending_ids = []
    for subscription in subscriptions:
        delivery, created = PushDelivery.objects.get_or_create(
            event=event, subscription=subscription, kind=kind,
            defaults={"operational_alert": event.operational_alert},
        )
        if created:
            pending_ids.append(delivery.id)
    if pending_ids:
        from .tasks import deliver_push_batch_task
        transaction.on_commit(lambda: deliver_push_batch_task.delay([str(i) for i in pending_ids]))
    return len(pending_ids)


def publish_event(event: NotificationEvent, *, actor=None) -> NotificationEvent:
    with transaction.atomic():
        event = NotificationEvent.objects.select_for_update().get(pk=event.pk)
        if event.status == NotificationEvent.Status.PUBLISHED:
            return event
        if event.status != NotificationEvent.Status.DRAFT:
            raise ValueError("Somente rascunhos podem ser publicados.")
        if not event.regions.exists() and not event.neighborhoods.exists():
            raise ValueError("Informe ao menos uma região ou bairro.")
        audience = preview_event_audience(event)
        event.status = NotificationEvent.Status.PUBLISHED
        event.published_at = timezone.now()
        event.audience_count = audience["users"]
        event.territory_snapshot = _snapshot(event)
        event.save(update_fields=["status", "published_at", "audience_count", "territory_snapshot", "updated_at"])
        NotificationEventAudit.objects.create(event=event, action="PUBLISHED", actor=actor, metadata=audience)
        logger.info(
            "notification_event_published event=%s origin=%s users=%s devices=%s",
            event.pk, event.origin, audience["users"], audience["devices"],
        )
        transaction.on_commit(lambda: fanout_event(event))
        return event


def create_camera_event(publication, event_kind: str) -> NotificationEvent:
    alert = publication.alert
    key = f"camera:{alert.pk}:{event_kind.lower()}"
    if event_kind == "RESOLVED":
        original = NotificationEvent.objects.filter(idempotency_key=f"camera:{alert.pk}:confirmed").first()
        event, created = NotificationEvent.objects.get_or_create(
            idempotency_key=key,
            defaults={
                "origin": NotificationEvent.Origin.CAMERA,
                "severity": NotificationEvent.Severity.INFO,
                "title": "Alerta de alagamento encerrado",
                "message": f"O alerta de alagamento em {alert.region.name} foi encerrado.",
                "destination_url": f"/cameras/{alert.camera_id}",
                "operational_alert": alert,
                "actor": publication.confirmed_by,
                "geometry": original.geometry if original else None,
            },
        )
    else:
        camera = alert.camera
        geometry = None
        if camera.latitude is not None and camera.longitude is not None:
            from django.contrib.gis.geos import Point
            geometry = Point(camera.longitude, camera.latitude, srid=4326)
        event, created = NotificationEvent.objects.get_or_create(
            idempotency_key=key,
            defaults={
                "origin": NotificationEvent.Origin.CAMERA,
                "severity": NotificationEvent.Severity.CRITICAL,
                "title": publication.title,
                "message": publication.message,
                "destination_url": f"/cameras/{alert.camera_id}",
                "operational_alert": alert,
                "actor": publication.confirmed_by,
                "geometry": geometry,
            },
        )
    if alert.region_id:
        event.regions.add(alert.region_id)
    if created:
        NotificationEventAudit.objects.create(event=event, action="CREATED", actor=publication.confirmed_by)
    return event


def create_flood_point_event(point, *, actor=None) -> NotificationEvent:
    event, created = NotificationEvent.objects.get_or_create(
        idempotency_key=f"flood-point:{point.pk}",
        defaults={
            "origin": NotificationEvent.Origin.FLOOD_POINT,
            "severity": NotificationEvent.Severity.CRITICAL,
            "title": "Novo ponto de alagamento",
            "message": f"Foi registrado um ponto de alagamento em {point.neighborhood.name}.",
            "destination_url": "/",
            "flood_point": point,
            "actor": actor,
            "geometry": point.footprint or point.location,
        },
    )
    neighborhoods = list(point.neighborhood_links.values_list("neighborhood_id", flat=True))
    if not neighborhoods and point.neighborhood_id:
        neighborhoods = [point.neighborhood_id]
    event.neighborhoods.add(*neighborhoods)
    region_ids = list(point.neighborhood_links.exclude(neighborhood__region_id=None).values_list("neighborhood__region_id", flat=True))
    event.regions.add(*region_ids)
    if created:
        NotificationEventAudit.objects.create(event=event, action="CREATED", actor=actor)
    return event


def _payload(delivery: PushDelivery) -> dict:
    if delivery.event_id:
        event = delivery.event
        return {
            "title": event.title,
            "body": event.message,
            "url": event.destination_url or "/",
            "event_id": str(event.pk),
            "origin": event.origin,
            "severity": event.severity,
            "state": event.status,
            "kind": delivery.kind,
        }
    alert = delivery.operational_alert
    confirmed = delivery.kind == PushDelivery.Kind.CONFIRMED
    camera_id = getattr(alert, "camera_id", None)
    region = getattr(alert, "region", None)
    region_name = getattr(region, "name", "região monitorada")
    return {
        "title": (
            "Alagamento confirmado por administrador"
            if confirmed
            else "Alerta de alagamento encerrado"
        ),
        "body": (
            f"Há um alagamento confirmado em {region_name}."
            if confirmed
            else f"O alerta de alagamento em {region_name} foi encerrado."
        ),
        "url": f"/cameras/{camera_id}" if camera_id else f"/regioes/{alert.region_id}",
        "alert_id": str(alert.pk),
        "kind": delivery.kind,
    }


def deliver_push(delivery_id, *, adapter=None) -> str:
    adapter = adapter or WebPushAdapter()
    with transaction.atomic():
        delivery = (
            PushDelivery.objects.select_for_update(of=("self",))
            .select_related("subscription", "event", "operational_alert__region")
            .get(pk=delivery_id)
        )
        if delivery.status in {PushDelivery.Status.SENT, PushDelivery.Status.EXPIRED}:
            return delivery.status
        delivery.attempts += 1
        subscription = delivery.subscription
        try:
            adapter.send(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                payload=_payload(delivery),
            )
        except PushDeliveryError as exc:
            if exc.status_code in {404, 410}:
                subscription.is_active = False
                subscription.expired_at = timezone.now()
                subscription.save(update_fields=["is_active", "expired_at", "updated_at"])
                delivery.status = PushDelivery.Status.EXPIRED
            else:
                delivery.status = PushDelivery.Status.FAILED
            delivery.last_error = str(exc)[:255]
            delivery.save(update_fields=["attempts", "status", "last_error", "updated_at"])
            logger.warning(
                "push_delivery_failed delivery=%s status=%s attempts=%s",
                delivery.pk, delivery.status, delivery.attempts,
            )
            return delivery.status
        delivery.status = PushDelivery.Status.SENT
        delivery.sent_at = timezone.now()
        delivery.last_error = ""
        delivery.save(
            update_fields=["attempts", "status", "sent_at", "last_error", "updated_at"]
        )
        logger.info("push_delivery_sent delivery=%s attempts=%s", delivery.pk, delivery.attempts)
        return delivery.status


def recover_failed_deliveries() -> int:
    ids = list(
        PushDelivery.objects.filter(
            status=PushDelivery.Status.FAILED,
            attempts__lt=settings.WEB_PUSH_MAX_ATTEMPTS,
            subscription__is_active=True,
        ).values_list("id", flat=True)[: settings.WEB_PUSH_RECOVERY_BATCH_SIZE]
    )
    if ids:
        PushDelivery.objects.filter(id__in=ids).update(status=PushDelivery.Status.PENDING)
        from .tasks import deliver_push_batch_task

        deliver_push_batch_task.delay([str(item) for item in ids])
    return len(ids)
