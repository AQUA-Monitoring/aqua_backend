from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.addressing.models import Address, City, Neighborhood, Region
from core.flood_camera_monitoring.infra.models import (
    AlertPublication,
    Camera,
    CameraOperationalSnapshot,
    FloodDetectionRecord,
    OperationalAlert,
)
from core.flood_camera_monitoring.services.analyze import AnalyzeAllCamerasService
from core.flood_camera_monitoring.services.operational_alerts import (
    AlertAdminRequired,
    AlertRegionRequired,
    InvalidAlertEvidence,
    InvalidAlertTransition,
    confirm_operational_alert,
    create_or_update_alert_for_detection,
    dismiss_operational_alert,
    resolve_operational_alert,
    sync_active_alert_regions_for_camera,
)
from core.users.infra.models import User


class OperationalAlertDomainTests(TestCase):
    def setUp(self):
        self.region = Region.objects.create(name="Região Norte", city="Joinville")
        self.camera = Camera.objects.create(
            status=Camera.CameraStatus.ACTIVE,
            description="Câmera Norte",
            region=self.region,
        )
        self.snapshot = CameraOperationalSnapshot.objects.create(
            camera=self.camera,
            stream_status=CameraOperationalSnapshot.StreamStatus.ONLINE,
            analysis_status=CameraOperationalSnapshot.AnalysisStatus.AVAILABLE,
            classification=CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION,
            model_status=CameraOperationalSnapshot.ModelStatus.READY,
            model_version="model-v1",
            frames=3,
            confidence=91.0,
            prob_normal=3.0,
            prob_medium=6.0,
            prob_flooded=91.0,
        )
        self.admin = User.objects.create(
            name="Admin", email="admin-alert@example.com", type=User.UserType.ADMIN
        )
        self.standard = User.objects.create(
            name="Pessoa", email="person-alert@example.com"
        )

    def detection(self, *, flooded=True, medium=False):
        return FloodDetectionRecord.objects.create(
            camera=self.camera,
            is_flooded=flooded,
            medium=medium,
            confidence=91.0,
            prob_normal=3.0,
            prob_medium=6.0,
            prob_flooded=91.0,
        )

    def test_continuous_strong_evidence_updates_single_active_alert(self):
        first = self.detection()
        alert, created = create_or_update_alert_for_detection(first, self.snapshot)
        second = self.detection()
        same_alert, created_again = create_or_update_alert_for_detection(
            second, self.snapshot
        )

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(same_alert.pk, alert.pk)
        self.assertEqual(OperationalAlert.objects.count(), 1)
        self.assertEqual(same_alert.initial_detection_id, first.pk)
        self.assertEqual(same_alert.latest_detection_id, second.pk)
        self.assertEqual(same_alert.transitions.count(), 1)

    def test_database_rejects_two_active_alerts_for_camera(self):
        detection = self.detection()
        create_or_update_alert_for_detection(detection, self.snapshot)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OperationalAlert.objects.create(
                camera=self.camera,
                region=self.region,
                initial_detection=detection,
                latest_detection=detection,
                first_detected_at=detection.created_at,
                last_detected_at=detection.created_at,
            )

    def test_intermediate_fallback_and_unavailable_evidence_are_rejected(self):
        intermediate = self.detection(flooded=False, medium=True)
        self.snapshot.classification = (
            CameraOperationalSnapshot.CameraClassification.INTERMEDIATE_INDICATION
        )
        self.snapshot.save()
        with self.assertRaises(InvalidAlertEvidence):
            create_or_update_alert_for_detection(intermediate, self.snapshot)

        strong = self.detection()
        for field, value in (
            ("model_status", CameraOperationalSnapshot.ModelStatus.FALLBACK),
            ("analysis_status", CameraOperationalSnapshot.AnalysisStatus.NO_FRAME),
            ("stream_status", CameraOperationalSnapshot.StreamStatus.UNAVAILABLE),
        ):
            self.snapshot.refresh_from_db()
            self.snapshot.classification = (
                CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION
            )
            self.snapshot.model_status = CameraOperationalSnapshot.ModelStatus.READY
            self.snapshot.analysis_status = (
                CameraOperationalSnapshot.AnalysisStatus.AVAILABLE
            )
            self.snapshot.stream_status = CameraOperationalSnapshot.StreamStatus.ONLINE
            setattr(self.snapshot, field, value)
            self.snapshot.save()
            with self.assertRaises(InvalidAlertEvidence):
                create_or_update_alert_for_detection(strong, self.snapshot)
        self.assertFalse(OperationalAlert.objects.exists())

    def test_confirm_creates_immutable_human_publication_and_audit(self):
        alert, _ = create_or_update_alert_for_detection(
            self.detection(), self.snapshot
        )
        callbacks = []
        with self.captureOnCommitCallbacks(execute=True):
            result = confirm_operational_alert(
                alert.pk,
                self.admin,
                reason="Verificado na câmera",
                on_published=lambda publication, event: callbacks.append(
                    (publication.pk, event)
                ),
            )

        self.assertEqual(result.alert.status, OperationalAlert.Status.CONFIRMED)
        self.assertEqual(result.publication.title, "Alagamento confirmado por administrador")
        self.assertEqual(result.publication.region, self.region)
        self.assertEqual(callbacks, [(result.publication.pk, "CONFIRMED")])
        transition = result.alert.transitions.last()
        self.assertEqual(transition.actor, self.admin)
        self.assertEqual(transition.reason, "Verificado na câmera")
        result.publication.title = "Alterado"
        with self.assertRaises(ValidationError):
            result.publication.save()

    def test_standard_user_and_repeated_transition_are_rejected(self):
        alert, _ = create_or_update_alert_for_detection(
            self.detection(), self.snapshot
        )
        with self.assertRaises(AlertAdminRequired):
            confirm_operational_alert(alert.pk, self.standard)
        confirm_operational_alert(alert.pk, self.admin)
        with self.assertRaises(InvalidAlertTransition):
            confirm_operational_alert(alert.pk, self.admin)

    def test_confirmation_requires_canonical_region(self):
        self.camera.region = None
        self.camera.save(update_fields=["region"])
        alert, _ = create_or_update_alert_for_detection(
            self.detection(), self.snapshot
        )
        with self.assertRaises(AlertRegionRequired):
            confirm_operational_alert(alert.pk, self.admin)

    def test_location_update_synchronizes_active_alert_region(self):
        alert, _ = create_or_update_alert_for_detection(self.detection(), self.snapshot)
        alert.region = None
        alert.save(update_fields=["region"])

        self.assertEqual(sync_active_alert_regions_for_camera(self.camera), 1)
        self.assertEqual(
            OperationalAlert.objects.get(pk=alert.pk).region,
            self.region,
        )

    def test_confirmation_repairs_region_from_camera_neighborhood(self):
        city, _ = City.objects.get_or_create(name="Joinville")
        self.region.city_ref = city
        self.region.save(update_fields=["city_ref"])
        neighborhood = Neighborhood.objects.create(
            name="Centro", city=city.name, city_ref=city, region=self.region
        )
        self.camera.region = None
        self.camera.city = city
        self.camera.neighborhood = neighborhood
        self.camera.save(update_fields=["region", "city", "neighborhood"])
        alert, _ = create_or_update_alert_for_detection(self.detection(), self.snapshot)

        result = confirm_operational_alert(alert.pk, self.admin)

        self.assertEqual(result.alert.region, self.region)
        self.assertEqual(
            OperationalAlert.objects.get(pk=alert.pk).region_id,
            self.region.id,
        )

    def test_confirmation_repairs_region_from_address_neighborhood(self):
        city, _ = City.objects.get_or_create(name="Joinville")
        self.region.city_ref = city
        self.region.save(update_fields=["city_ref"])
        neighborhood = Neighborhood.objects.create(
            name="Centro", city=city.name, city_ref=city, region=self.region
        )
        self.camera.region = None
        self.camera.neighborhood = None
        self.camera.address = Address.objects.create(
            street="Rua da Câmera",
            city=city.name,
            city_ref=city,
            neighborhood=neighborhood,
        )
        self.camera.save(update_fields=["region", "neighborhood", "address"])
        alert, _ = create_or_update_alert_for_detection(self.detection(), self.snapshot)

        result = confirm_operational_alert(alert.pk, self.admin)

        self.assertEqual(result.alert.region, self.region)

    def test_confirmation_rejects_conflicting_territorial_regions(self):
        city, _ = City.objects.get_or_create(name="Joinville")
        self.region.city_ref = city
        self.region.save(update_fields=["city_ref"])
        other_region = Region.objects.create(
            name="Região conflitante", city=city.name, city_ref=city
        )
        neighborhood = Neighborhood.objects.create(
            name="Centro", city=city.name, city_ref=city, region=self.region
        )
        self.camera.region = other_region
        self.camera.city = city
        self.camera.neighborhood = neighborhood
        self.camera.save(update_fields=["region", "city", "neighborhood"])
        alert, _ = create_or_update_alert_for_detection(self.detection(), self.snapshot)

        with self.assertRaises(AlertRegionRequired):
            confirm_operational_alert(alert.pk, self.admin)

    def test_inactive_camera_and_inactive_region_are_rejected(self):
        detection = self.detection()
        self.camera.status = Camera.CameraStatus.INACTIVE
        self.camera.save(update_fields=["status"])
        with self.assertRaises(InvalidAlertEvidence):
            create_or_update_alert_for_detection(detection, self.snapshot)

        self.camera.status = Camera.CameraStatus.ACTIVE
        self.camera.save(update_fields=["status"])
        alert, _ = create_or_update_alert_for_detection(detection, self.snapshot)
        self.region.is_active = False
        self.region.save(update_fields=["is_active"])
        with self.assertRaises(AlertRegionRequired):
            confirm_operational_alert(alert.pk, self.admin)

    def test_dismiss_does_not_publish_and_resolution_records_push_choice(self):
        dismissed, _ = create_or_update_alert_for_detection(
            self.detection(), self.snapshot
        )
        dismiss_operational_alert(dismissed.pk, self.admin, reason="Falso positivo")
        self.assertFalse(AlertPublication.objects.exists())

        active, _ = create_or_update_alert_for_detection(
            self.detection(), self.snapshot
        )
        confirm_operational_alert(active.pk, self.admin)
        callbacks = []
        with self.captureOnCommitCallbacks(execute=True):
            resolved = resolve_operational_alert(
                active.pk,
                self.admin,
                notify_subscribers=False,
                on_published=lambda publication, event: callbacks.append(event),
            )
        self.assertEqual(resolved.status, OperationalAlert.Status.RESOLVED)
        self.assertEqual(callbacks, [])
        self.assertFalse(resolved.transitions.last().metadata["notify_subscribers"])

    def test_analysis_persistence_opens_only_for_strong_classification(self):
        summary = {
            "decision_flooded": 88.0,
            "mean_normal": 4.0,
            "mean_medium": 8.0,
            "mean_flooded": 88.0,
            "chosen_bytes": None,
        }
        saved = AnalyzeAllCamerasService._persist_detection(
            self.camera,
            self.snapshot,
            [],
            summary,
            CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION,
        )
        self.assertTrue(saved)
        self.assertEqual(OperationalAlert.objects.count(), 1)

        OperationalAlert.objects.update(status=OperationalAlert.Status.RESOLVED)
        saved = AnalyzeAllCamerasService._persist_detection(
            self.camera,
            self.snapshot,
            [],
            summary,
            CameraOperationalSnapshot.CameraClassification.INTERMEDIATE_INDICATION,
        )
        self.assertTrue(saved)
        self.assertEqual(OperationalAlert.objects.count(), 1)
