from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from core.flood_camera_monitoring.infra.models import (
    Camera,
    CameraOperationalSnapshot,
    FloodDetectionRecord,
)
from core.flood_camera_monitoring.services.analyze import AnalyzeAllCamerasService
from core.flood_camera_monitoring.services.predict import PredictAllCamerasService
from core.flood_camera_monitoring.tasks import (
    analyze_all_cameras_task,
    refresh_all_and_cache_task,
    refresh_predict_all_cache_task,
)


class OfflineCameraAnalysisExclusionTests(TestCase):
    def setUp(self):
        Camera.objects.update(status=Camera.CameraStatus.INACTIVE)
        self.offline_camera = Camera.objects.create(
            status=Camera.CameraStatus.OFFLINE,
            description="Câmera offline somente transmissão",
            video_hls="https://camera.invalid/offline.m3u8",
        )
        self.offline_snapshot = CameraOperationalSnapshot.objects.create(
            camera=self.offline_camera,
            stream_status=CameraOperationalSnapshot.StreamStatus.ONLINE,
            stream_checked_at=timezone.now(),
            analysis_status=CameraOperationalSnapshot.AnalysisStatus.AVAILABLE,
            classification=(
                CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION
            ),
            prob_normal=5.0,
            prob_medium=5.0,
            prob_flooded=90.0,
            confidence=90.0,
            frames=3,
            analyzed_at=timezone.now(),
            model_status=CameraOperationalSnapshot.ModelStatus.READY,
            model_version="previous-model",
        )

    @staticmethod
    def guarded_service():
        return AnalyzeAllCamerasService(
            stream_factory=Mock(side_effect=AssertionError("stream opened")),
            classifier_factory=Mock(
                side_effect=AssertionError("classifier initialized")
            ),
            artifact_inspector=Mock(
                side_effect=AssertionError("model artifact inspected")
            ),
        )

    def assert_offline_was_not_processed(self, service, *, records_before=0):
        service.stream_factory.assert_not_called()
        service.classifier_factory.assert_not_called()
        service.artifact_inspector.assert_not_called()
        self.assertEqual(FloodDetectionRecord.objects.count(), records_before)

    def test_analyze_service_skips_offline_before_capture_or_model_resolution(self):
        records_before = FloodDetectionRecord.objects.count()
        service = self.guarded_service()

        data, saved = service.run_and_collect()

        self.assertEqual((data, saved), ([], 0))
        self.assert_offline_was_not_processed(
            service, records_before=records_before
        )

    def test_predict_service_and_cache_task_omit_offline_camera(self):
        self.assertEqual(PredictAllCamerasService().run(), [])

        with patch(
            "core.flood_camera_monitoring.tasks.cache_set_json"
        ) as cache_set_json:
            count = refresh_predict_all_cache_task.run()

        self.assertEqual(count, 0)
        cache_set_json.assert_called_once()
        cached_payload = cache_set_json.call_args.args[1]
        self.assertEqual(cached_payload["data"], [])

    def test_analysis_tasks_keep_offline_camera_out_of_capture_and_cache(self):
        records_before = FloodDetectionRecord.objects.count()

        legacy_service = self.guarded_service()
        with patch(
            "core.flood_camera_monitoring.services.analyze.AnalyzeAllCamerasService",
            return_value=legacy_service,
        ):
            saved = analyze_all_cameras_task.run()
        self.assertEqual(saved, 0)
        self.assert_offline_was_not_processed(
            legacy_service, records_before=records_before
        )

        unified_service = self.guarded_service()
        with (
            patch(
                "core.flood_camera_monitoring.services.analyze.AnalyzeAllCamerasService",
                return_value=unified_service,
            ),
            patch(
                "core.flood_camera_monitoring.tasks.cache_set_json"
            ) as cache_set_json,
        ):
            count = refresh_all_and_cache_task.run()

        self.assertEqual(count, 0)
        self.assert_offline_was_not_processed(
            unified_service, records_before=records_before
        )
        cached_payload = cache_set_json.call_args.args[1]
        self.assertEqual(cached_payload["data"], [])
