from datetime import timedelta
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import Mock

from django.core import signing
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.users.infra.models import User
from core.flood_camera_monitoring.infra.models import Camera, CameraAnalysisRun, CameraEvidence
from core.flood_camera_monitoring.services.automatic_alerts import eligible_for_publication


class MonitoringAPITests(TestCase):
    def setUp(self):
        self.camera = Camera.objects.create(status=1)
        self.admin = User.objects.create(name='Operator', email='monitor@example.com', type='admin')
        self.client = APIClient()
        self.url = f'/api/flood_monitoring/cameras/{self.camera.pk}/monitoring/'

    def test_anonymous_and_standard_cannot_read_evidence(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_authenticate(User.objects.create(name='Public', email='public@example.com'))
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_manual_intensity_requires_reason_and_is_audited(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(self.url, {'level': 'CRITICAL'}).status_code, 400)
        self.assertEqual(self.client.post(self.url, {'level': 'CRITICAL', 'reason': 'Water visible'}).status_code, 200)
        self.camera.refresh_from_db()
        self.assertEqual(self.camera.monitoring.level, 'CRITICAL')

    def test_evidence_link_is_user_bound(self):
        run = CameraAnalysisRun.objects.create(camera=self.camera, started_at=timezone.now(), level='WATCH')
        item = CameraEvidence.objects.create(run=run, relative_path='frame.jpg', size=3, sha256='a'*64,
            captured_at=timezone.now(), expires_at=timezone.now()+timedelta(days=90))
        with TemporaryDirectory() as folder, override_settings(FLOOD_PRIVATE_EVIDENCE_ROOT=folder):
            (Path(folder)/'frame.jpg').write_bytes(b'jpg')
            self.client.force_authenticate(self.admin)
            bad = signing.dumps({'id': str(item.pk), 'user': 'another-user'}, salt='flood-evidence')
            self.assertEqual(self.client.get(f'/api/flood_monitoring/evidence/{item.pk}/?token={bad}').status_code, 403)
            good = signing.dumps({'id': str(item.pk), 'user': str(self.admin.pk)}, salt='flood-evidence')
            response = self.client.get(f'/api/flood_monitoring/evidence/{item.pk}/?token={good}')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Cache-Control'], 'private, no-store')
            self.assertEqual(b''.join(response.streaming_content), b'jpg')

    def test_three_strong_separate_windows_need_context_and_images(self):
        now = timezone.now()
        runs = []
        for index in range(3):
            run = Mock(model_version='one-model', error_code='', frames=[{'flooded': 95}],
                context={'neighbor_signal': True, 'quality_usable': True},
                started_at=now+timedelta(seconds=index*40), finished_at=now+timedelta(seconds=index*40+20))
            run.evidence.filter.return_value.exists.return_value = True
            runs.append(run)
        self.assertTrue(eligible_for_publication(runs))
        runs[1].context['neighbor_signal'] = False
        self.assertFalse(eligible_for_publication(runs))
        runs[1].context['neighbor_signal'] = True
        runs[1].evidence.filter.return_value.exists.return_value = False
        self.assertFalse(eligible_for_publication(runs))
        self.assertFalse(eligible_for_publication(runs[:1]))
