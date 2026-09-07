from unittest.mock import patch

from django.test import TestCase, override_settings
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.test import APIClient

from core.addressing.models import Address, City, Neighborhood, Region
from core.flood_camera_monitoring.infra.models import (
    Camera,
    FloodDetectionRecord,
    OperationalAlert,
)
from core.users.infra.models import User

from ..adapters import PushDeliveryError
from ..models import NotificationEvent, PushDelivery, PushSubscription, RegionSubscription, SavedPlace
from ..services import deliver_push, fanout_alert, preview_event_audience, publish_event


class NotificationFixture(TestCase):
    def setUp(self):
        self.city = City.objects.create(name="Joinville Notifica")
        self.region = Region.objects.create(
            name="Região Norte", city=self.city.name, city_ref=self.city
        )
        self.other_region = Region.objects.create(
            name="Região Sul", city=self.city.name, city_ref=self.city
        )
        self.admin = User.objects.create(
            name="Admin", email="notification-admin@example.test", type=User.UserType.ADMIN
        )
        self.user = User.objects.create(
            name="Morador", email="notification-user@example.test"
        )
        self.other_user = User.objects.create(
            name="Outro", email="notification-other@example.test"
        )
        self.camera = Camera.objects.create(
            status=Camera.CameraStatus.ACTIVE,
            description="Câmera Norte",
            region=self.region,
        )
        self.detection = FloodDetectionRecord.objects.create(
            camera=self.camera,
            is_flooded=True,
            confidence=0.94,
            prob_normal=0.02,
            prob_medium=0.04,
            prob_flooded=0.94,
        )
        self.alert = OperationalAlert.objects.create(
            camera=self.camera,
            region=self.region,
            initial_detection=self.detection,
            latest_detection=self.detection,
            first_detected_at=self.detection.created_at,
            last_detected_at=self.detection.created_at,
            evidence={
                "classification": "FLOOD_INDICATION",
                "confidence": 0.94,
                "probabilities": {"normal": 0.02, "medium": 0.04, "flooded": 0.94},
                "model_version": "test-model",
            },
        )


class SubscriptionApiTests(NotificationFixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_authentication_is_required(self):
        self.assertEqual(self.client.get("/api/region-subscriptions/").status_code, 401)
        self.assertEqual(self.client.post("/api/push-subscriptions/", {}, format="json").status_code, 401)

    def test_region_subscription_is_idempotent_and_can_be_deleted(self):
        self.client.force_authenticate(self.user)
        payload = {"region_id": str(self.region.id)}
        created = self.client.post("/api/region-subscriptions/", payload, format="json")
        repeated = self.client.post("/api/region-subscriptions/", payload, format="json")
        self.assertEqual(created.status_code, 201)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(RegionSubscription.objects.count(), 1)
        listed = self.client.get("/api/region-subscriptions/")
        self.assertEqual(listed.data["results"][0]["region"]["name"], "Região Norte")
        self.assertEqual(
            listed.data["results"][0]["region"]["city"],
            {"id": str(self.city.id), "name": self.city.name},
        )
        deleted = self.client.delete(
            f"/api/region-subscriptions/?region_id={self.region.id}"
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(
            self.client.delete(f"/api/region-subscriptions/?region_id={self.region.id}").status_code,
            204,
        )

    def test_push_subscription_is_upserted_without_exposing_keys(self):
        self.client.force_authenticate(self.user)
        payload = {
            "endpoint": "https://push.example.test/device-1",
            "keys": {"p256dh": "public-device-key", "auth": "auth-secret"},
        }
        response = self.client.post("/api/push-subscriptions/", payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("endpoint", response.data)
        self.assertNotIn("keys", response.data)
        subscription = PushSubscription.objects.get()
        self.assertEqual(subscription.user, self.user)
        self.assertEqual(subscription.p256dh, "public-device-key")
        renewed = self.client.post("/api/push-subscriptions/", payload, format="json")
        self.assertEqual(renewed.status_code, 200)
        deleted = self.client.delete(
            "/api/push-subscriptions/?endpoint=https%3A%2F%2Fpush.example.test%2Fdevice-1"
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(
            self.client.delete(
                "/api/push-subscriptions/?endpoint=https%3A%2F%2Fpush.example.test%2Fdevice-1"
            ).status_code,
            204,
        )

    @override_settings(WEB_PUSH_ENABLED=False, WEB_PUSH_VAPID_PUBLIC_KEY="not-public")
    def test_config_does_not_expose_key_while_disabled(self):
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/push-subscriptions/config/")
        self.assertEqual(
            response.data,
            {"enabled": False, "public_key": "", "application_server_key": ""},
        )


class PushDeliveryServiceTests(NotificationFixture):
    def setUp(self):
        super().setUp()
        RegionSubscription.objects.create(user=self.user, region=self.region)
        RegionSubscription.objects.create(user=self.other_user, region=self.other_region)
        self.subscription = PushSubscription.objects.create(
            user=self.user,
            endpoint="https://push.example.test/device-1",
            p256dh="key",
            auth="auth",
        )
        PushSubscription.objects.create(
            user=self.other_user,
            endpoint="https://push.example.test/device-2",
            p256dh="key-2",
            auth="auth-2",
        )

    @patch("core.notifications.tasks.deliver_push_batch_task.delay")
    def test_fanout_targets_region_and_is_idempotent(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(fanout_alert(self.alert, PushDelivery.Kind.CONFIRMED), 1)
            self.assertEqual(fanout_alert(self.alert, PushDelivery.Kind.CONFIRMED), 0)
        self.assertEqual(PushDelivery.objects.count(), 1)
        self.assertEqual(PushDelivery.objects.get().subscription, self.subscription)
        delay.assert_called_once()

    def test_successful_delivery_records_sent_without_private_data_in_payload(self):
        delivery = PushDelivery.objects.create(
            operational_alert=self.alert,
            subscription=self.subscription,
            kind=PushDelivery.Kind.CONFIRMED,
        )

        class Adapter:
            def send(inner_self, *, subscription_info, payload):
                self.assertEqual(subscription_info["endpoint"], self.subscription.endpoint)
                self.assertNotIn("endpoint", payload)
                self.assertNotIn("p256dh", str(payload))

        self.assertEqual(deliver_push(delivery.id, adapter=Adapter()), PushDelivery.Status.SENT)
        delivery.refresh_from_db()
        self.assertEqual(delivery.attempts, 1)
        self.assertIsNotNone(delivery.sent_at)

    def test_gone_subscription_is_expired_and_deactivated(self):
        delivery = PushDelivery.objects.create(
            operational_alert=self.alert,
            subscription=self.subscription,
            kind=PushDelivery.Kind.CONFIRMED,
        )

        class GoneAdapter:
            def send(self, **kwargs):
                raise PushDeliveryError("endpoint expirado", status_code=410)

        self.assertEqual(deliver_push(delivery.id, adapter=GoneAdapter()), PushDelivery.Status.EXPIRED)
        delivery.refresh_from_db()
        self.subscription.refresh_from_db()
        self.assertFalse(self.subscription.is_active)
        self.assertEqual(delivery.last_error, "endpoint expirado")

    def test_failure_is_sanitized_and_marked_for_retry(self):
        delivery = PushDelivery.objects.create(
            operational_alert=self.alert,
            subscription=self.subscription,
            kind=PushDelivery.Kind.CONFIRMED,
        )

        class FailedAdapter:
            def send(self, **kwargs):
                raise PushDeliveryError("falha temporária")

        self.assertEqual(
            deliver_push(delivery.id, adapter=FailedAdapter()),
            PushDelivery.Status.FAILED,
        )
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, PushDelivery.Status.FAILED)
        self.assertEqual(delivery.last_error, "falha temporária")


class UnifiedNotificationApiTests(NotificationFixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_saved_places_are_private_and_radius_is_validated(self):
        self.client.force_authenticate(self.user)
        with patch("core.notifications.serializers.TerritoryResolver.resolve_point") as resolve:
            resolve.return_value.city = self.city
            resolve.return_value.region = self.region
            resolve.return_value.neighborhood = None
            created = self.client.post("/api/saved-places/", {
                "name": "Casa", "latitude": -26.3, "longitude": -48.8, "radius_km": 3,
            }, format="json")
        self.assertEqual(created.status_code, 201)
        self.client.force_authenticate(self.other_user)
        self.assertEqual(self.client.get("/api/saved-places/").data["count"], 0)
        invalid = self.client.post("/api/saved-places/", {
            "name": "Longe", "latitude": -26.3, "longitude": -48.8, "radius_km": 25,
        }, format="json")
        self.assertEqual(invalid.status_code, 400)

    @patch("core.notifications.tasks.deliver_push_batch_task.delay")
    def test_manual_event_preview_and_publish_deduplicate_overlapping_preferences(self, delay):
        RegionSubscription.objects.create(user=self.user, region=self.region)
        PushSubscription.objects.create(user=self.user, endpoint="https://push.example.test/unified", p256dh="key", auth="auth")
        self.client.force_authenticate(self.admin)
        response = self.client.post("/api/notification-events/", {
            "title": "Chuva intensa", "message": "Evite a região.", "severity": "CRITICAL",
            "region_ids": [str(self.region.id)], "neighborhood_ids": [],
        }, format="json")
        self.assertEqual(response.status_code, 201)
        event = NotificationEvent.objects.get(pk=response.data["id"])
        self.assertEqual(preview_event_audience(event), {"users": 1, "devices": 1})
        with self.captureOnCommitCallbacks(execute=True):
            published = self.client.post(f"/api/notification-events/{event.id}/publish/")
        self.assertEqual(published.status_code, 200)
        self.assertEqual(PushDelivery.objects.filter(event=event).count(), 1)
        delay.assert_called_once()

    def test_standard_user_cannot_manage_manual_events(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get("/api/notification-events/").status_code, 403)

    def test_saved_place_distance_respects_each_configured_radius(self):
        farther_user = User.objects.create(name="Raio dez", email="radius-ten@example.test")
        SavedPlace.objects.create(user=self.user, name="Raio 1", location=Point(-48.8, -26.3), radius_km=1, city=self.city)
        SavedPlace.objects.create(user=self.other_user, name="Raio 3", location=Point(-48.78, -26.3), radius_km=3, city=self.city)
        SavedPlace.objects.create(user=self.admin, name="Fora do raio", location=Point(-48.78, -26.3), radius_km=1, city=self.city)
        SavedPlace.objects.create(user=farther_user, name="Raio 10", location=Point(-48.75, -26.3), radius_km=10, city=self.city)
        event = NotificationEvent.objects.create(
            origin="MANUAL", title="Teste", message="Teste", idempotency_key="distance:test",
            geometry=Point(-48.8, -26.3),
        )
        event.regions.add(self.region)
        self.assertEqual(preview_event_audience(event)["users"], 3)


class OperationalAlertApiTests(NotificationFixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_standard_user_cannot_access_admin_feed(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get("/api/operational-alerts/").status_code, 403)
        self.assertEqual(
            self.client.post(f"/api/operational-alerts/{self.alert.id}/confirm/", {}).status_code,
            403,
        )

    def test_admin_feed_is_paginated_and_does_not_expose_stream_urls(self):
        self.camera.video_hls = "https://private.example.test/stream.m3u8"
        self.camera.save(update_fields=["video_hls"])
        self.client.force_authenticate(self.admin)
        response = self.client.get("/api/operational-alerts/?status=OPEN_INDICATION")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        alert = response.data["results"][0]
        self.assertNotIn("video_hls", alert["camera"])
        self.assertEqual(alert["camera"]["administrative_status"], "ACTIVE")
        self.assertEqual(alert["camera"]["detail_path"], f"/cameras/{self.camera.id}")
        self.assertEqual(alert["region"]["city"]["id"], str(self.city.id))
        self.assertEqual(alert["evidence"]["initial_detection_id"], str(self.detection.id))
        self.assertEqual(alert["evidence"]["latest_detection_id"], str(self.detection.id))
        self.assertIsNone(alert["evidence"]["image_url"])
        self.assertEqual(
            alert["detection_records"]["initial"]["id"], str(self.detection.id)
        )
        self.assertEqual(
            alert["detection_records"]["latest"]["probabilities"]["flooded"],
            self.detection.prob_flooded,
        )

    def test_admin_feed_uses_the_camera_canonical_region_for_legacy_alert(self):
        self.alert.region = None
        self.alert.save(update_fields=["region"])
        self.client.force_authenticate(self.admin)

        response = self.client.get("/api/operational-alerts/?status=OPEN_INDICATION")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["results"][0]["region"],
            {
                "id": str(self.region.id),
                "name": self.region.name,
                "city": {"id": str(self.city.id), "name": self.city.name},
            },
        )

    def test_admin_feed_filters_by_canonical_address_neighborhood(self):
        canonical_neighborhood = Neighborhood.objects.create(
            name="Centro",
            city=self.city.name,
            city_ref=self.city,
            region=self.region,
        )
        legacy_neighborhood = Neighborhood.objects.create(
            name="Bairro legado",
            city=self.city.name,
            city_ref=self.city,
            region=self.other_region,
        )
        self.camera.address = Address.objects.create(
            street="Rua das Águas",
            city=self.city.name,
            city_ref=self.city,
            neighborhood=canonical_neighborhood,
        )
        self.camera.neighborhood = legacy_neighborhood
        self.camera.save(update_fields=["address", "neighborhood"])
        self.client.force_authenticate(self.admin)

        canonical_response = self.client.get(
            f"/api/operational-alerts/?neighborhood_id={canonical_neighborhood.id}"
        )
        legacy_response = self.client.get(
            f"/api/operational-alerts/?neighborhood_id={legacy_neighborhood.id}"
        )

        self.assertEqual(canonical_response.status_code, 200)
        self.assertEqual(canonical_response.data["count"], 1)
        self.assertEqual(
            canonical_response.data["results"][0]["id"],
            str(self.alert.id),
        )
        self.assertEqual(legacy_response.status_code, 200)
        self.assertEqual(legacy_response.data["count"], 0)

    def test_admin_feed_filters_by_legacy_neighborhood_only_without_address(self):
        neighborhood = Neighborhood.objects.create(
            name="Bairro sem endereço",
            city=self.city.name,
            city_ref=self.city,
            region=self.region,
        )
        self.camera.neighborhood = neighborhood
        self.camera.save(update_fields=["neighborhood"])
        self.client.force_authenticate(self.admin)

        response = self.client.get(
            f"/api/operational-alerts/?neighborhood_id={neighborhood.id}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(self.alert.id))

    def test_admin_feed_rejects_invalid_neighborhood_uuid(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(
            "/api/operational-alerts/?neighborhood_id=nao-e-uuid"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("neighborhood_id", response.data)

    def test_neighborhood_filter_combines_with_existing_filters_and_pagination(self):
        neighborhood = Neighborhood.objects.create(
            name="Bairro paginado",
            city=self.city.name,
            city_ref=self.city,
            region=self.region,
        )
        self.camera.neighborhood = neighborhood
        self.camera.save(update_fields=["neighborhood"])
        second_camera = Camera.objects.create(
            status=Camera.CameraStatus.ACTIVE,
            description="Segunda câmera",
            neighborhood=neighborhood,
            region=self.region,
        )
        second_detection = FloodDetectionRecord.objects.create(
            camera=second_camera,
            is_flooded=True,
            confidence=0.91,
            prob_normal=0.03,
            prob_medium=0.06,
            prob_flooded=0.91,
        )
        second_alert = OperationalAlert.objects.create(
            camera=second_camera,
            region=self.region,
            initial_detection=second_detection,
            latest_detection=second_detection,
            first_detected_at=second_detection.created_at,
            last_detected_at=second_detection.created_at,
            evidence={"classification": "FLOOD_INDICATION"},
        )
        self.client.force_authenticate(self.admin)

        first_page = self.client.get(
            "/api/operational-alerts/",
            {
                "neighborhood_id": str(neighborhood.id),
                "region_id": str(self.region.id),
                "status": OperationalAlert.Status.OPEN_INDICATION,
                "page_size": 1,
            },
        )
        second_page = self.client.get(
            "/api/operational-alerts/",
            {
                "neighborhood_id": str(neighborhood.id),
                "region_id": str(self.region.id),
                "status": OperationalAlert.Status.OPEN_INDICATION,
                "page_size": 1,
                "page": 2,
            },
        )
        empty_combination = self.client.get(
            "/api/operational-alerts/",
            {
                "neighborhood_id": str(neighborhood.id),
                "region_id": str(self.other_region.id),
            },
        )

        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.data["count"], 2)
        self.assertEqual(len(first_page.data["results"]), 1)
        self.assertIsNotNone(first_page.data["next"])
        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(second_page.data["count"], 2)
        self.assertEqual(len(second_page.data["results"]), 1)
        self.assertCountEqual(
            [
                first_page.data["results"][0]["id"],
                second_page.data["results"][0]["id"],
            ],
            [str(self.alert.id), str(second_alert.id)],
        )
        self.assertEqual(empty_combination.status_code, 200)
        self.assertEqual(empty_combination.data["count"], 0)
        self.assertEqual(empty_combination.data["results"], [])

    @patch("core.notifications.tasks.deliver_push_batch_task.delay")
    def test_confirmation_is_unique_and_second_request_conflicts(self, delay):
        RegionSubscription.objects.create(user=self.user, region=self.region)
        PushSubscription.objects.create(
            user=self.user,
            endpoint="https://push.example.test/device-confirm",
            p256dh="key",
            auth="auth",
        )
        self.client.force_authenticate(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/operational-alerts/{self.alert.id}/confirm/", {}, format="json"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "CONFIRMED")
        self.assertEqual(PushDelivery.objects.count(), 1)
        repeated = self.client.post(
            f"/api/operational-alerts/{self.alert.id}/confirm/", {}, format="json"
        )
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(PushDelivery.objects.count(), 1)
