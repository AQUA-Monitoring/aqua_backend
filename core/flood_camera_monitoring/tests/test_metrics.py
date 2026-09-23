from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core.flood_camera_monitoring.infra.models import (
    Camera,
    CameraOperationalSnapshot,
)


METRICS_URL = "/internal/metrics"
METRICS_TOKEN = "metrics-test-token"


@override_settings(
    INTERNAL_METRICS_ENABLED=True,
    INTERNAL_METRICS_TOKEN=METRICS_TOKEN,
)
class InternalMetricsTests(TestCase):
    def setUp(self):
        # Seed migrations create operational camera snapshots. Metrics tests
        # need a controlled dataset so their exact cardinality assertions are
        # independent from the production seed catalog.
        CameraOperationalSnapshot.objects.all().delete()

    def authorization(self, token: str = METRICS_TOKEN) -> dict[str, str]:
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def create_snapshot(self, **overrides) -> CameraOperationalSnapshot:
        camera = Camera.objects.create(
            status=Camera.CameraStatus.ACTIVE,
            description=overrides.pop("description", "Camera metrics test"),
            video_hls=overrides.pop(
                "video_hls",
                "https://private-camera.invalid/live.m3u8",
            ),
        )
        defaults = {
            "stream_status": CameraOperationalSnapshot.StreamStatus.ONLINE,
            "analysis_status": CameraOperationalSnapshot.AnalysisStatus.AVAILABLE,
            "classification": (
                CameraOperationalSnapshot.CameraClassification.NO_INDICATION
            ),
            "model_status": CameraOperationalSnapshot.ModelStatus.READY,
            "model_version": "model-v1",
            "analyzed_at": timezone.now(),
            "stream_checked_at": timezone.now(),
        }
        defaults.update(overrides)
        return CameraOperationalSnapshot.objects.create(camera=camera, **defaults)

    def test_authorized_response_uses_prometheus_content_type_and_controlled_labels(self):
        now = timezone.now()
        self.create_snapshot(
            analysis_status=CameraOperationalSnapshot.AnalysisStatus.NO_FRAME,
            stream_status=CameraOperationalSnapshot.StreamStatus.UNAVAILABLE,
            # Stale/corrupt historical values must not make fallback look like
            # a real classification.
            classification=(
                CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION
            ),
            model_status=CameraOperationalSnapshot.ModelStatus.FALLBACK,
            model_version=None,
            error_code="MODEL_FALLBACK",
            analyzed_at=now - timedelta(seconds=120),
            stream_checked_at=now - timedelta(seconds=60),
        )

        response = self.client.get(METRICS_URL, **self.authorization())
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response["Content-Type"].startswith("text/plain; version=0.0.4")
        )
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn(
            'aqua_model_fallback_active{scope="operational"} 1.0',
            body,
        )
        self.assertIn(
            'aqua_camera_analysis_status_total{scope="operational",status="NO_FRAME"} 1.0',
            body,
        )
        self.assertIn(
            'aqua_camera_stream_status_total{scope="operational",status="UNAVAILABLE"} 1.0',
            body,
        )
        self.assertIn(
            'aqua_camera_error_total{error_code="MODEL_FALLBACK",scope="operational"} 1.0',
            body,
        )
        self.assertIn(
            'aqua_camera_classification_total{classification="UNKNOWN",scope="operational"} 1.0',
            body,
        )
        self.assertIn(
            'aqua_camera_classification_total{classification="FLOOD_INDICATION",scope="operational"} 0.0',
            body,
        )
        self.assertNotIn('scope="demo"', body)

    def test_model_ready_versions_and_ages_are_aggregated_without_camera_labels(self):
        fixed_now = timezone.now()
        first = self.create_snapshot(
            model_version="model-v1",
            analyzed_at=fixed_now - timedelta(seconds=20),
            stream_checked_at=fixed_now - timedelta(seconds=10),
        )
        self.create_snapshot(
            model_version="model-v1",
            analyzed_at=fixed_now - timedelta(seconds=70),
            stream_checked_at=fixed_now - timedelta(seconds=40),
        )

        with patch(
            "core.flood_camera_monitoring.services.metrics.timezone.now",
            return_value=fixed_now,
        ):
            response = self.client.get(METRICS_URL, **self.authorization())
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'aqua_model_ready{scope="operational",version="model-v1"} 2.0',
            body,
        )
        self.assertIn(
            'aqua_camera_analysis_age_seconds{scope="operational"} 70.0',
            body,
        )
        self.assertIn(
            'aqua_camera_stream_check_age_seconds{scope="operational"} 40.0',
            body,
        )
        self.assertNotIn(str(first.camera_id), body)
        self.assertNotIn("camera_id=", body)

    def test_model_unavailable_is_explicit_and_not_a_ready_inference(self):
        self.create_snapshot(
            analysis_status=(
                CameraOperationalSnapshot.AnalysisStatus.MODEL_UNAVAILABLE
            ),
            classification=(
                CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION
            ),
            model_status=CameraOperationalSnapshot.ModelStatus.UNAVAILABLE,
            model_version=None,
            error_code="MODEL_MISSING",
        )

        response = self.client.get(METRICS_URL, **self.authorization())
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'aqua_model_unavailable{scope="operational"} 1.0',
            body,
        )
        self.assertIn(
            'aqua_model_ready{scope="operational",version="UNKNOWN"} 0.0',
            body,
        )
        self.assertIn(
            'aqua_camera_classification_total{classification="FLOOD_INDICATION",scope="operational"} 0.0',
            body,
        )

    def test_uncontrolled_database_labels_are_collapsed_and_sensitive_fields_are_absent(self):
        private_url = "https://private.example/model/checkpoint.pth"
        snapshot = self.create_snapshot(
            description="Secret camera description",
            video_hls="https://private.example/live.m3u8?token=secret",
            model_version=private_url,
            error_code="PRIVATE_PROVIDER_TIMEOUT_camera-123",
        )

        response = self.client.get(METRICS_URL, **self.authorization())
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'aqua_model_ready{scope="operational",version="OTHER"} 1.0',
            body,
        )
        self.assertIn(
            'aqua_camera_error_total{error_code="OTHER",scope="operational"} 1.0',
            body,
        )
        self.assertNotIn(private_url, body)
        self.assertNotIn("Secret camera description", body)
        self.assertNotIn("token=secret", body)
        self.assertNotIn(str(snapshot.camera_id), body)
        self.assertNotIn("PRIVATE_PROVIDER_TIMEOUT", body)

    def test_missing_timestamps_are_not_reported_as_fresh(self):
        self.create_snapshot(
            analysis_status=CameraOperationalSnapshot.AnalysisStatus.NOT_ANALYZED,
            stream_status=CameraOperationalSnapshot.StreamStatus.UNKNOWN,
            classification=None,
            model_status=CameraOperationalSnapshot.ModelStatus.UNKNOWN,
            model_version=None,
            analyzed_at=None,
            stream_checked_at=None,
        )

        response = self.client.get(METRICS_URL, **self.authorization())
        body = response.content.decode("utf-8")

        self.assertIn(
            'aqua_camera_analysis_age_seconds{scope="operational"} NaN',
            body,
        )
        self.assertIn(
            'aqua_camera_stream_check_age_seconds{scope="operational"} NaN',
            body,
        )

    def test_collection_failure_is_503_without_exception_details(self):
        with patch(
            "core.flood_camera_monitoring.presentation.metrics_views.render_camera_ml_metrics",
            side_effect=RuntimeError("database at private-host failed"),
        ):
            response = self.client.get(METRICS_URL, **self.authorization())
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 503)
        self.assertIn(
            'aqua_camera_metrics_collection_success{scope="operational"} 0.0',
            body,
        )
        self.assertNotIn("private-host", body)

    def test_missing_or_wrong_token_is_forbidden(self):
        self.assertEqual(self.client.get(METRICS_URL).status_code, 403)
        self.assertEqual(
            self.client.get(
                METRICS_URL,
                **self.authorization("wrong-token"),
            ).status_code,
            403,
        )

    @override_settings(INTERNAL_METRICS_ENABLED=False)
    def test_disabled_endpoint_is_not_found(self):
        response = self.client.get(METRICS_URL, **self.authorization())
        self.assertEqual(response.status_code, 404)

    @override_settings(INTERNAL_METRICS_TOKEN="")
    def test_missing_configured_token_is_not_found(self):
        response = self.client.get(METRICS_URL, **self.authorization())
        self.assertEqual(response.status_code, 404)


class MetricsLightweightImportTests(TestCase):
    def test_metrics_view_does_not_import_opencv_or_torch(self):
        project_root = Path(__file__).resolve().parents[3]
        script = """
import importlib
import os
import sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
importlib.import_module('core.flood_camera_monitoring.presentation.metrics_views')
assert not {'cv2', 'torch', 'torchvision'}.intersection(sys.modules)
"""
        subprocess.run(
            [sys.executable, "-c", script],
            cwd=project_root,
            env=os.environ.copy(),
            check=True,
            capture_output=True,
            text=True,
        )
