from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core.flood_camera_monitoring.services.operational_snapshot import (
    begin_analysis,
    begin_capture,
    mark_stream_online,
)
from core.flood_camera_monitoring.services.analyze import (
    AnalyzeAllCamerasService,
)
from core.flood_camera_monitoring.services.predict import (
    PredictAllCamerasService,
)
from core.flood_camera_monitoring.services.model_artifact import (
    ModelArtifactInfo,
)
from core.flood_camera_monitoring.infra.models import (
    Camera,
    CameraOperationalSnapshot,
)


class FakeStream:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.closed = False

    def grab(self):
        return self.outputs.pop(0) if self.outputs else None

    def close(self):
        self.closed = True


class CameraOperationalSnapshotTests(TestCase):
    def setUp(self):
        self.camera = Camera.objects.create(
            status=Camera.CameraStatus.ACTIVE,
            description="Câmera de teste",
            video_hls="https://camera.invalid/live.m3u8",
        )
        Camera.objects.exclude(pk=self.camera.pk).update(
            status=Camera.CameraStatus.INACTIVE
        )
        CameraOperationalSnapshot.objects.create(camera=self.camera)

    def snapshot(self):
        return CameraOperationalSnapshot.objects.get(camera=self.camera)

    @staticmethod
    def available_artifact():
        return ModelArtifactInfo(
            available=True,
            path=Path("/tmp/checkpoint-not-loaded.pth"),
            version="model-v1",
            error_code=None,
        )

    def service(self, stream, *, classifier=None, artifact=None):
        classifier = classifier or Mock(_fallback=False)
        return AnalyzeAllCamerasService(
            sample_frames=1,
            sample_interval_ms=0,
            warmup_drops=0,
            stream_factory=Mock(return_value=stream),
            classifier_factory=Mock(return_value=classifier),
            artifact_inspector=Mock(
                return_value=artifact or self.available_artifact()
            ),
        )

    def assert_invalid_result(self, snapshot):
        self.assertIsNone(snapshot.classification)
        self.assertIsNone(snapshot.prob_normal)
        self.assertIsNone(snapshot.prob_medium)
        self.assertIsNone(snapshot.prob_flooded)
        self.assertIsNone(snapshot.confidence)
        self.assertIsNone(snapshot.frames)

    def test_explicit_intermediate_transitions_are_persisted(self):
        snapshot = self.snapshot()

        begin_capture(snapshot)
        snapshot.refresh_from_db()
        self.assertEqual(
            snapshot.stream_status,
            CameraOperationalSnapshot.StreamStatus.CHECKING,
        )
        self.assertEqual(
            snapshot.analysis_status,
            CameraOperationalSnapshot.AnalysisStatus.NOT_ANALYZED,
        )

        mark_stream_online(snapshot)
        begin_analysis(snapshot, frames=3)
        snapshot.refresh_from_db()
        self.assertEqual(
            snapshot.stream_status,
            CameraOperationalSnapshot.StreamStatus.ONLINE,
        )
        self.assertEqual(
            snapshot.analysis_status,
            CameraOperationalSnapshot.AnalysisStatus.RUNNING,
        )
        self.assertEqual(snapshot.frames, 3)

    def test_no_frame_never_becomes_zero_probability(self):
        stream = FakeStream([None])
        service = self.service(stream)

        data, saved = service.run_and_collect()

        snapshot = self.snapshot()
        self.assertEqual(saved, 0)
        self.assertTrue(stream.closed)
        self.assertEqual(
            snapshot.stream_status,
            CameraOperationalSnapshot.StreamStatus.UNAVAILABLE,
        )
        self.assertEqual(
            snapshot.analysis_status,
            CameraOperationalSnapshot.AnalysisStatus.NO_FRAME,
        )
        self.assertEqual(snapshot.error_code, "NO_FRAME")
        self.assert_invalid_result(snapshot)
        self.assertIsNone(data[0]["probabilities"]["flooded"])
        service.artifact_inspector.assert_not_called()
        service.classifier_factory.assert_not_called()

    def test_capture_error_is_explicit_and_clears_result(self):
        service = AnalyzeAllCamerasService(
            sample_frames=1,
            sample_interval_ms=0,
            warmup_drops=0,
            stream_factory=Mock(side_effect=RuntimeError("stream failed")),
            classifier_factory=Mock(),
            artifact_inspector=Mock(),
        )

        service.run_and_collect()

        snapshot = self.snapshot()
        self.assertEqual(
            snapshot.stream_status,
            CameraOperationalSnapshot.StreamStatus.UNAVAILABLE,
        )
        self.assertEqual(
            snapshot.analysis_status,
            CameraOperationalSnapshot.AnalysisStatus.ERROR,
        )
        self.assertEqual(snapshot.error_code, "STREAM_CAPTURE_ERROR")
        self.assert_invalid_result(snapshot)
        service.artifact_inspector.assert_not_called()
        service.classifier_factory.assert_not_called()

    def test_missing_model_invalidates_result_but_keeps_stream_online(self):
        stream = FakeStream([b"frame"])
        artifact = ModelArtifactInfo(
            available=False,
            path=Path("/tmp/missing.pth"),
            version=None,
            error_code="MODEL_MISSING",
        )
        service = self.service(stream, artifact=artifact)

        service.run_and_collect()

        snapshot = self.snapshot()
        self.assertEqual(
            snapshot.stream_status,
            CameraOperationalSnapshot.StreamStatus.ONLINE,
        )
        self.assertEqual(
            snapshot.analysis_status,
            CameraOperationalSnapshot.AnalysisStatus.MODEL_UNAVAILABLE,
        )
        self.assertEqual(
            snapshot.model_status,
            CameraOperationalSnapshot.ModelStatus.UNAVAILABLE,
        )
        self.assertEqual(snapshot.error_code, "MODEL_MISSING")
        self.assert_invalid_result(snapshot)
        service.classifier_factory.assert_not_called()

    def test_fallback_is_not_exposed_as_inference(self):
        stream = FakeStream([b"frame"])
        service = self.service(stream, classifier=Mock(_fallback=True))

        service.run_and_collect()

        snapshot = self.snapshot()
        self.assertEqual(
            snapshot.analysis_status,
            CameraOperationalSnapshot.AnalysisStatus.MODEL_UNAVAILABLE,
        )
        self.assertEqual(
            snapshot.model_status,
            CameraOperationalSnapshot.ModelStatus.FALLBACK,
        )
        self.assertEqual(snapshot.error_code, "MODEL_FALLBACK")
        self.assert_invalid_result(snapshot)

    @patch(
        "core.flood_camera_monitoring.services.analyze.aggregate_predictions"
    )
    def test_success_persists_available_snapshot(self, aggregate_predictions):
        aggregate_predictions.return_value = (
            {
                "strong": False,
                "medium_flag": False,
                "mean_normal": 80.0,
                "mean_medium": 5.0,
                "mean_flooded": 15.0,
                "decision_flooded": 15.0,
                "frames_count": 1,
                "chosen_bytes": b"frame",
            },
            [],
        )
        service = self.service(FakeStream([b"frame"]))

        data, saved = service.run_and_collect()

        snapshot = self.snapshot()
        self.assertEqual(saved, 0)
        self.assertEqual(
            snapshot.analysis_status,
            CameraOperationalSnapshot.AnalysisStatus.AVAILABLE,
        )
        self.assertEqual(
            snapshot.classification,
            CameraOperationalSnapshot.CameraClassification.NO_INDICATION,
        )
        self.assertEqual(snapshot.model_status, CameraOperationalSnapshot.ModelStatus.READY)
        self.assertEqual(snapshot.model_version, "model-v1")
        self.assertEqual(snapshot.prob_flooded, 15.0)
        self.assertEqual(snapshot.confidence, 80.0)
        self.assertEqual(snapshot.frames, 1)
        self.assertEqual(data[0]["probabilities"]["normal"], 80.0)

    @patch(
        "core.flood_camera_monitoring.services.analyze.aggregate_predictions",
        side_effect=RuntimeError("inference failed"),
    )
    def test_inference_error_clears_previous_values(self, _aggregate_predictions):
        snapshot = self.snapshot()
        snapshot.classification = (
            CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION
        )
        snapshot.prob_flooded = 99.0
        snapshot.confidence = 99.0
        snapshot.frames = 3
        snapshot.save()

        service = self.service(FakeStream([b"frame"]))
        service.run_and_collect()

        snapshot.refresh_from_db()
        self.assertEqual(
            snapshot.analysis_status,
            CameraOperationalSnapshot.AnalysisStatus.ERROR,
        )
        self.assertEqual(snapshot.model_status, CameraOperationalSnapshot.ModelStatus.READY)
        self.assertEqual(snapshot.error_code, "INFERENCE_ERROR")
        self.assert_invalid_result(snapshot)

    def test_demo_loop_is_not_added_to_operational_snapshots(self):
        self.camera.video_hls = "loop:media:demo.mp4"
        self.camera.save(update_fields=["video_hls", "updated_at"])
        CameraOperationalSnapshot.objects.filter(camera=self.camera).delete()
        stream_factory = Mock()
        classifier_factory = Mock()
        service = AnalyzeAllCamerasService(
            stream_factory=stream_factory,
            classifier_factory=classifier_factory,
        )

        data, saved = service.run_and_collect()

        self.assertEqual((data, saved), ([], 0))
        self.assertFalse(
            CameraOperationalSnapshot.objects.filter(camera=self.camera).exists()
        )
        stream_factory.assert_not_called()
        classifier_factory.assert_not_called()

    @override_settings(FLOOD_ANALYSIS_STALE_SECONDS=600)
    def test_predict_all_reads_snapshot_and_calculates_stale_without_inference(self):
        snapshot = self.snapshot()
        snapshot.stream_status = CameraOperationalSnapshot.StreamStatus.ONLINE
        snapshot.analysis_status = CameraOperationalSnapshot.AnalysisStatus.AVAILABLE
        snapshot.classification = (
            CameraOperationalSnapshot.CameraClassification.NO_INDICATION
        )
        snapshot.prob_normal = 90.0
        snapshot.prob_medium = 5.0
        snapshot.prob_flooded = 5.0
        snapshot.confidence = 90.0
        snapshot.frames = 3
        snapshot.model_status = CameraOperationalSnapshot.ModelStatus.READY
        snapshot.model_version = "model-v1"
        snapshot.analyzed_at = timezone.now() - timedelta(seconds=601)
        snapshot.save()

        result = PredictAllCamerasService().run()[0]

        snapshot.refresh_from_db()
        self.assertEqual(result["status"], CameraOperationalSnapshot.AnalysisStatus.STALE)
        self.assertEqual(result["probabilities"]["flooded"], 5.0)
        self.assertEqual(
            snapshot.analysis_status,
            CameraOperationalSnapshot.AnalysisStatus.AVAILABLE,
        )

    def test_projection_hides_dirty_values_from_invalid_states(self):
        snapshot = self.snapshot()
        invalid_states = (
            (
                CameraOperationalSnapshot.AnalysisStatus.NO_FRAME,
                CameraOperationalSnapshot.ModelStatus.READY,
            ),
            (
                CameraOperationalSnapshot.AnalysisStatus.MODEL_UNAVAILABLE,
                CameraOperationalSnapshot.ModelStatus.FALLBACK,
            ),
            (
                CameraOperationalSnapshot.AnalysisStatus.ERROR,
                CameraOperationalSnapshot.ModelStatus.READY,
            ),
        )

        for analysis_status, model_status in invalid_states:
            with self.subTest(
                analysis_status=analysis_status,
                model_status=model_status,
            ):
                snapshot.analysis_status = analysis_status
                snapshot.model_status = model_status
                snapshot.classification = (
                    CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION
                )
                snapshot.prob_normal = 1.0
                snapshot.prob_medium = 1.0
                snapshot.prob_flooded = 98.0
                snapshot.confidence = 98.0
                snapshot.frames = 3
                snapshot.model_version = "model-v1"
                snapshot.analyzed_at = timezone.now()
                snapshot.save()

                result = PredictAllCamerasService().run()[0]

                self.assertIsNone(result["classification"])
                self.assertIsNone(result["confidence"])
                self.assertIsNone(result["meta"]["frames"])
                self.assertTrue(
                    all(
                        value is None
                        for value in result["probabilities"].values()
                    )
                )

    def test_offline_camera_keeps_stream_state_but_hides_previous_analysis(self):
        snapshot = self.snapshot()
        snapshot.stream_status = CameraOperationalSnapshot.StreamStatus.ONLINE
        snapshot.analysis_status = CameraOperationalSnapshot.AnalysisStatus.AVAILABLE
        snapshot.classification = (
            CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION
        )
        snapshot.prob_normal = 5.0
        snapshot.prob_medium = 5.0
        snapshot.prob_flooded = 90.0
        snapshot.confidence = 90.0
        snapshot.frames = 3
        snapshot.model_status = CameraOperationalSnapshot.ModelStatus.READY
        snapshot.model_version = "model-v1"
        snapshot.analyzed_at = timezone.now()
        snapshot.save()
        self.camera.status = Camera.CameraStatus.OFFLINE
        self.camera.save(update_fields=["status"])

        result = PredictAllCamerasService().run()
        self.assertEqual(result, [])

        from core.flood_camera_monitoring.services.operational_snapshot import (
            snapshot_prediction_payload,
        )

        detail_projection = snapshot_prediction_payload(self.camera, snapshot)
        self.assertEqual(
            detail_projection["meta"]["stream_status"],
            CameraOperationalSnapshot.StreamStatus.ONLINE,
        )
        self.assertEqual(
            detail_projection["status"],
            CameraOperationalSnapshot.AnalysisStatus.NOT_ANALYZED,
        )
        self.assertIsNone(detail_projection["classification"])
        self.assertTrue(
            all(
                value is None
                for value in detail_projection["probabilities"].values()
            )
        )

    def test_inactive_camera_hides_stream_and_previous_analysis(self):
        snapshot = self.snapshot()
        snapshot.stream_status = CameraOperationalSnapshot.StreamStatus.ONLINE
        snapshot.analysis_status = CameraOperationalSnapshot.AnalysisStatus.AVAILABLE
        snapshot.classification = (
            CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION
        )
        snapshot.prob_normal = 5.0
        snapshot.prob_medium = 5.0
        snapshot.prob_flooded = 90.0
        snapshot.confidence = 90.0
        snapshot.frames = 3
        snapshot.model_status = CameraOperationalSnapshot.ModelStatus.READY
        snapshot.model_version = "model-v1"
        snapshot.analyzed_at = timezone.now()
        snapshot.save()
        self.camera.status = Camera.CameraStatus.INACTIVE
        self.camera.save(update_fields=["status"])

        from core.flood_camera_monitoring.services.operational_snapshot import (
            snapshot_prediction_payload,
        )

        detail_projection = snapshot_prediction_payload(self.camera, snapshot)
        self.assertEqual(
            detail_projection["meta"]["stream_status"],
            CameraOperationalSnapshot.StreamStatus.UNKNOWN,
        )
        self.assertEqual(
            detail_projection["status"],
            CameraOperationalSnapshot.AnalysisStatus.NOT_ANALYZED,
        )
        self.assertIsNone(detail_projection["classification"])
        self.assertTrue(
            all(
                value is None
                for value in detail_projection["probabilities"].values()
            )
        )

    def test_current_threshold_mapping_is_preserved(self):
        classify = AnalyzeAllCamerasService._classification
        self.assertEqual(
            classify({"strong": True, "medium_flag": False}),
            CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION,
        )
        self.assertEqual(
            classify({"strong": False, "medium_flag": True}),
            CameraOperationalSnapshot.CameraClassification.INTERMEDIATE_INDICATION,
        )
        self.assertEqual(
            classify({"strong": False, "medium_flag": False}),
            CameraOperationalSnapshot.CameraClassification.NO_INDICATION,
        )
