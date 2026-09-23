from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core.flood_camera_monitoring.infra.models import (
    Camera, CameraMonitoringState, CameraAnalysisRun, CameraEvidence,
)
from core.flood_camera_monitoring.services.adaptive_monitoring import reserve_due, analyze_reserved, expire_evidence
from core.flood_camera_monitoring.services.automatic_alerts import eligible_for_publication
from core.flood_camera_monitoring.services.model_registry import promotion_errors


class AdaptiveMonitoringTests(TestCase):
    def setUp(self):
        Camera.objects.all().update(status=2)
        self.camera = Camera.objects.create(status=1, description='Terminal Central - regression')

    def test_leases_prevent_duplicate_dispatch(self):
        first = reserve_due()
        self.assertEqual(len(first), 1)
        self.assertEqual(reserve_due(), [])
        CameraMonitoringState.objects.filter(pk=self.camera.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        self.assertEqual(len(reserve_due()), 1)

    def test_preserves_normal_capture_and_duplicate_task_is_noop(self):
        reserved = reserve_due()[0]
        def fake_run(service):
            service.capture_observer(self.camera, [b'evidence'])
            return ([{'classification': 'NO_INDICATION'}], 0)
        with TemporaryDirectory() as folder, override_settings(FLOOD_PRIVATE_EVIDENCE_ROOT=folder), patch(
            'core.flood_camera_monitoring.services.analyze.AnalyzeAllCamerasService.run_and_collect', fake_run):
            analyze_reserved(*reserved[:2])
            analyze_reserved(*reserved[:2])
            self.assertEqual(CameraAnalysisRun.objects.count(), 1)
            evidence = CameraEvidence.objects.get()
            self.assertEqual((Path(folder)/evidence.relative_path).read_bytes(), b'evidence')
            self.assertFalse(eligible_for_publication(list(CameraAnalysisRun.objects.all())) )

    def test_retention_keeps_audit_record(self):
        run = CameraAnalysisRun.objects.create(camera=self.camera, started_at=timezone.now(), level='NORMAL')
        with TemporaryDirectory() as folder, override_settings(FLOOD_PRIVATE_EVIDENCE_ROOT=folder):
            path = Path(folder)/'frame.jpg'
            path.write_bytes(b'image')
            item = CameraEvidence.objects.create(run=run, relative_path='frame.jpg', sha256='0'*64,
                size=5, captured_at=timezone.now(), expires_at=timezone.now()-timedelta(days=1))
            self.assertEqual(expire_evidence(), 1)
            self.assertFalse(path.exists())
            item.refresh_from_db()
            self.assertIsNotNone(item.deleted_at)
            self.assertEqual(expire_evidence(), 0)

    def test_unvalidated_model_cannot_be_promoted(self):
        self.assertGreater(len(promotion_errors({})), 3)
