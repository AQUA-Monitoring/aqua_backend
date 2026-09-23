import json
from unittest.mock import ANY, Mock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.flood_camera_monitoring.infra.demo_stream_client import (
    DemoStreamUnavailable,
)
from core.flood_camera_monitoring.infra.models import FloodDetectionRecord
from core.flood_camera_monitoring.services.evaluation import EvalConfig, capture_frames
from core.flood_camera_monitoring.services.stream_prediction import (
    FloodProbabilities,
    FloodSeverity,
    PredictionResult,
)
from core.flood_camera_monitoring.presentation.demo_views import (
    _release_prediction_lock,
    _representative_descriptor,
    _select_representative_frame,
    _store_representative_frame,
)
from core.users.infra.models import User


def stream_payload(
    *, state="auto", session_id="session-1", with_segment=True, expected_state="flooded"
):
    payload = {
        "ok": True,
        "status": "ready",
        "session_id": session_id,
        "demo_state": state,
        "available_states": ["auto", "normal", "flooded"],
        "scenario": {
            "scenario_id": "test",
            "segment_seconds": 2,
            "phases": [],
        },
        "current_phase": "flood phase",
        "hls_url": "http://localhost:8088/hls/playlist.m3u8",
        "segment": None,
        "source": {
            "type": "uploader",
            "mode": state,
            "status": "ready",
            "attachment_key": "private-key",
            "version": "private-version",
        },
        "sources": {state: {"attachment_key": "private-key"}},
    }
    if with_segment:
        payload["segment"] = {
            "sequence": 2,
            "phase": "flood phase",
            "expected_state": expected_state,
            "internal_url": "http://demo-stream:8088/hls/seg_000000002.ts",
        }
    return payload


def prediction_result(
    state: FloodSeverity,
    confidence: float,
    *,
    normal: float,
    medium: float,
    flooded: float,
) -> PredictionResult:
    return PredictionResult(
        is_flooded=state is FloodSeverity.FLOODED,
        severity=state,
        confidence=confidence,
        probabilities=FloodProbabilities(
            normal=normal,
            medium=medium,
            flooded=flooded,
        ),
        meta={"private": "must-not-be-serialized"},
    )


def cached_prediction(sequence, *, session_id="session-1", version="model-v1"):
    return {
        "schema_version": 2,
        "session_id": session_id,
        "demo_state": "auto",
        "segment": {
            "sequence": sequence,
            "phase": "flood phase",
            "expected_state": "flooded",
        },
        "prediction": {
            "state": "flooded",
            "confidence": 85.0,
            "probabilities": {"normal": 10.0, "medium": 5.0, "flooded": 85.0},
            "frames": 3,
            "samples": [],
        },
        "validation": {"expected": "flooded", "actual": "flooded", "match": True},
        "model": {"ready": True, "fallback": False, "version": version},
    }


class DemoSegmentCaptureTests(SimpleTestCase):
    @staticmethod
    def _stream_with(frames):
        stream = Mock()
        stream.grab.side_effect = [*frames, None]
        return stream

    @patch(
        "core.flood_camera_monitoring.services.evaluation.OpenCVVideoStream"
    )
    def test_capture_keeps_no_frames_when_segment_cannot_be_decoded(self, stream_type):
        stream = self._stream_with([])
        stream_type.return_value = stream

        self.assertEqual(
            capture_frames("internal-segment", EvalConfig(sample_frames=3, warmup_drops=0)),
            [],
        )
        stream.close.assert_called_once_with()


class DemoRepresentativeFrameTests(SimpleTestCase):
    @staticmethod
    def _stream_with(frames):
        stream = Mock()
        stream.grab.side_effect = [*frames, None]
        return stream

    def test_selects_highest_probability_for_each_final_state(self):
        frames = [b"normal", b"medium", b"flooded"]
        assessments = [
            prediction_result(
                FloodSeverity.NORMAL,
                80.0,
                normal=80.0,
                medium=10.0,
                flooded=10.0,
            ),
            prediction_result(
                FloodSeverity.MEDIUM,
                70.0,
                normal=20.0,
                medium=70.0,
                flooded=10.0,
            ),
            prediction_result(
                FloodSeverity.FLOODED,
                90.0,
                normal=5.0,
                medium=5.0,
                flooded=90.0,
            ),
        ]

        self.assertEqual(
            _select_representative_frame(frames, assessments, "normal"),
            b"normal",
        )
        self.assertEqual(
            _select_representative_frame(frames, assessments, "medium"),
            b"medium",
        )
        self.assertEqual(
            _select_representative_frame(frames, assessments, "flooded"),
            b"flooded",
        )

    @patch(
        "core.flood_camera_monitoring.presentation.demo_views.get_binary_redis"
    )
    def test_stores_jpeg_and_metadata_separately_with_fixed_ttl(self, redis_factory):
        pipeline = redis_factory.return_value.pipeline.return_value
        pipeline.execute.return_value = [True, True, True]

        descriptor = _store_representative_frame(
            b"\xff\xd8jpeg\xff\xd9",
            "session-1",
            2,
            "model-v1",
        )

        self.assertIsNotNone(descriptor)
        self.assertEqual(descriptor["content_type"], "image/jpeg")
        self.assertEqual(descriptor["session_id"], "session-1")
        self.assertEqual(descriptor["sequence"], 2)
        self.assertEqual(descriptor["model_version"], "model-v1")
        self.assertNotIn("internal", json.dumps(descriptor))
        set_calls = pipeline.set.call_args_list
        self.assertEqual(len(set_calls), 3)
        self.assertEqual(set_calls[0].args[1], b"\xff\xd8jpeg\xff\xd9")
        self.assertTrue(all(call.kwargs["ex"] == 30 for call in set_calls))
        self.assertNotEqual(set_calls[0].args[0], set_calls[1].args[0])

    @patch(
        "core.flood_camera_monitoring.presentation.demo_views.get_binary_redis",
        side_effect=RuntimeError("redis down"),
    )
    def test_redis_failure_omits_image_without_raising(self, _redis_factory):
        self.assertIsNone(
            _store_representative_frame(b"jpeg", "session-1", 2, "model-v1")
        )

    @patch("core.flood_camera_monitoring.presentation.demo_views.get_redis")
    def test_prediction_lock_release_is_atomic(self, redis_factory):
        _release_prediction_lock("prediction-key", "owner-token")

        redis_factory.return_value.eval.assert_called_once()
        script, key_count, key, token = redis_factory.return_value.eval.call_args.args
        self.assertIn('redis.call("get", KEYS[1])', script)
        self.assertIn('redis.call("del", KEYS[1])', script)
        self.assertEqual(
            (key_count, key, token),
            (1, "prediction-key:lock", "owner-token"),
        )

    @patch(
        "core.flood_camera_monitoring.presentation.demo_views.get_binary_redis"
    )
    def test_cached_descriptor_requires_matching_metadata_and_jpeg(
        self, redis_factory
    ):
        redis_client = redis_factory.return_value
        redis_client.get.side_effect = [
            b"0123456789abcdef0123456789abcdef",
            json.dumps(
                {
                    "session_id": "session-1",
                    "sequence": 2,
                    "model_version": "model-v1",
                    "expires_at": "2026-07-23T02:00:00+00:00",
                }
            ).encode(),
        ]
        redis_client.exists.return_value = 1

        descriptor = _representative_descriptor("session-1", 2, "model-v1")

        self.assertEqual(descriptor["sequence"], 2)
        self.assertEqual(descriptor["model_version"], "model-v1")
        self.assertTrue(descriptor["url"].endswith(
            "0123456789abcdef0123456789abcdef"
        ))

        redis_client.reset_mock()
        redis_client.get.side_effect = [
            b"0123456789abcdef0123456789abcdef",
            None,
        ]
        self.assertIsNone(
            _representative_descriptor("session-1", 2, "model-v1")
        )

    @patch(
        "core.flood_camera_monitoring.services.evaluation.OpenCVVideoStream"
    )
    def test_capture_keeps_partial_one_or_two_frame_segments(self, stream_type):
        for decoded in ([b"one"], [b"one", b"two"]):
            with self.subTest(decoded=len(decoded)):
                stream = self._stream_with(decoded)
                stream_type.return_value = stream

                frames = capture_frames(
                    "internal-segment",
                    EvalConfig(sample_frames=3, warmup_drops=0),
                )

                self.assertEqual(frames, decoded)
                stream.close.assert_called_once_with()

    @patch(
        "core.flood_camera_monitoring.services.evaluation.OpenCVVideoStream"
    )
    def test_capture_retains_literal_last_three_frames_in_temporal_order(
        self, stream_type
    ):
        stream = self._stream_with([b"one", b"two", b"three", b"four", b"five"])
        stream_type.return_value = stream

        frames = capture_frames(
            "internal-segment",
            EvalConfig(sample_frames=3, warmup_drops=0),
        )

        self.assertEqual(frames, [b"three", b"four", b"five"])
        stream.close.assert_called_once_with()

    @patch(
        "core.flood_camera_monitoring.services.evaluation.OpenCVVideoStream"
    )
    def test_capture_closes_stream_when_decode_raises(self, stream_type):
        stream = Mock()
        stream.grab.side_effect = RuntimeError("decode failed")
        stream_type.return_value = stream

        with self.assertRaisesRegex(RuntimeError, "decode failed"):
            capture_frames(
                "internal-segment",
                EvalConfig(sample_frames=3, warmup_drops=0),
            )

        stream.close.assert_called_once_with()


@override_settings(
    DEMO_ENABLED=True,
    DEMO_STREAM_INTERNAL_URL="http://demo-stream:8089",
    DEMO_CONTROL_TOKEN="test-token",
)
class DemoApiTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create(
            name="Admin",
            email="admin@example.com",
            type=User.UserType.ADMIN,
        )
        self.standard = User.objects.create(
            name="Standard",
            email="standard@example.com",
            type=User.UserType.STANDARD,
        )

    @patch("core.flood_camera_monitoring.presentation.demo_control_views._client")
    def test_status_is_public_and_hides_internal_segment_url(self, client_factory):
        client_factory.return_value.get_state.return_value = stream_payload()

        response = self.client.get("/api/flood_monitoring/demo")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["enabled"])
        self.assertNotIn("internal_url", response.data["segment"])
        self.assertNotIn("sources", response.data)
        self.assertEqual(
            response.data["source"],
            {"type": "uploader", "mode": "auto", "status": "ready"},
        )

    @override_settings(DEMO_ENABLED=False)
    @patch("core.flood_camera_monitoring.presentation.demo_control_views._client")
    def test_disabled_demo_status_does_not_call_sidecar(self, client_factory):
        response = self.client.get("/api/flood_monitoring/demo")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"enabled": False, "status": "disabled"})
        client_factory.assert_not_called()

    def test_anonymous_user_cannot_change_state(self):
        response = self.client.post(
            "/api/flood_monitoring/demo/state", {"state": "normal"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_standard_user_cannot_change_state(self):
        self.client.force_authenticate(self.standard)
        response = self.client.post(
            "/api/flood_monitoring/demo/state", {"state": "normal"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("core.flood_camera_monitoring.presentation.demo_control_views._client")
    def test_admin_changes_state_and_receives_new_session(self, client_factory):
        sidecar = client_factory.return_value
        sidecar.get_state.return_value = stream_payload(session_id="old-session")
        sidecar.set_state.return_value = stream_payload(
            state="normal", session_id="new-session"
        )
        self.client.force_authenticate(self.admin)

        response = self.client.post(
            "/api/flood_monitoring/demo/state", {"state": "normal"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["session_id"], "new-session")
        self.assertEqual(response.data["demo_state"], "normal")
        sidecar.set_state.assert_called_once_with("normal")

    @override_settings(DEMO_ENABLED=False)
    @patch("core.flood_camera_monitoring.presentation.demo_control_views._client")
    def test_disabled_demo_rejects_admin_state_change(self, client_factory):
        self.client.force_authenticate(self.admin)

        response = self.client.post(
            "/api/flood_monitoring/demo/state", {"state": "normal"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["detail"], "Demo stream is disabled")
        client_factory.assert_not_called()

    @patch("core.flood_camera_monitoring.presentation.demo_control_views.logger")
    @patch("core.flood_camera_monitoring.presentation.demo_control_views._client")
    def test_admin_state_change_is_audited(self, client_factory, logger):
        sidecar = client_factory.return_value
        sidecar.get_state.return_value = stream_payload(session_id="old-session")
        sidecar.set_state.return_value = stream_payload(
            state="normal", session_id="new-session"
        )
        self.client.force_authenticate(self.admin)

        response = self.client.post(
            "/api/flood_monitoring/demo/state", {"state": "normal"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        logger.info.assert_called_once()

    def test_invalid_state_is_rejected_before_sidecar_call(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            "/api/flood_monitoring/demo/state", {"state": "paused"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("core.flood_camera_monitoring.presentation.demo_control_views._client")
    def test_admin_cannot_select_a_phase_missing_from_scenario(self, client_factory):
        payload = stream_payload()
        payload["available_states"] = ["auto", "flooded"]
        client_factory.return_value.get_state.return_value = payload
        self.client.force_authenticate(self.admin)

        response = self.client.post(
            "/api/flood_monitoring/demo/state", {"state": "normal"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        client_factory.return_value.set_state.assert_not_called()

    @patch("core.flood_camera_monitoring.presentation.demo_control_views._client")
    def test_sidecar_unavailable_returns_503(self, client_factory):
        client_factory.return_value.get_state.side_effect = DemoStreamUnavailable("down")

        response = self.client.get("/api/flood_monitoring/demo")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @patch("core.flood_camera_monitoring.presentation.demo_views._store_cache")
    @patch("core.flood_camera_monitoring.presentation.demo_views._cached", return_value=None)
    @patch("core.flood_camera_monitoring.presentation.demo_views.aggregate_predictions")
    @patch("core.flood_camera_monitoring.presentation.demo_views.capture_frames")
    @patch("core.flood_camera_monitoring.presentation.demo_views.get_default_classifier")
    @patch("core.flood_camera_monitoring.presentation.demo_views._client")
    def test_prediction_is_public_compares_label_and_does_not_persist(
        self,
        client_factory,
        classifier_factory,
        capture,
        aggregate,
        cached,
        store_cache,
    ):
        client_factory.return_value.get_state.return_value = stream_payload()
        classifier_factory.return_value = Mock(_fallback=False)
        capture.return_value = [b"one", b"two", b"three"]
        assessments = [
            prediction_result(
                FloodSeverity.NORMAL,
                80.0,
                normal=80.0,
                medium=10.0,
                flooded=10.0,
            ),
            prediction_result(
                FloodSeverity.MEDIUM,
                55.0,
                normal=20.0,
                medium=55.0,
                flooded=25.0,
            ),
            prediction_result(
                FloodSeverity.FLOODED,
                85.0,
                normal=10.0,
                medium=5.0,
                flooded=85.0,
            ),
        ]
        aggregate.return_value = (
            {
                "strong": True,
                "medium_flag": False,
                "decision_flooded": 85.0,
                "mean_normal": 10.0,
                "mean_medium": 5.0,
                "mean_flooded": 85.0,
                "frames_count": 3,
            },
            assessments,
        )

        response = self.client.get("/api/flood_monitoring/demo/predict")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["validation"]["match"])
        self.assertEqual(response.data["prediction"]["state"], "flooded")
        self.assertEqual(response.data["schema_version"], 2)
        self.assertEqual(response.data["prediction"]["frames"], 3)
        self.assertEqual(
            response.data["prediction"]["samples"],
            [
                {
                    "index": 0,
                    "state": "normal",
                    "confidence": 80.0,
                    "probabilities": {
                        "normal": 80.0,
                        "medium": 10.0,
                        "flooded": 10.0,
                    },
                },
                {
                    "index": 1,
                    "state": "medium",
                    "confidence": 55.0,
                    "probabilities": {
                        "normal": 20.0,
                        "medium": 55.0,
                        "flooded": 25.0,
                    },
                },
                {
                    "index": 2,
                    "state": "flooded",
                    "confidence": 85.0,
                    "probabilities": {
                        "normal": 10.0,
                        "medium": 5.0,
                        "flooded": 85.0,
                    },
                },
            ],
        )
        self.assertNotIn("meta", response.data["prediction"]["samples"][0])
        self.assertEqual(FloodDetectionRecord.objects.count(), 0)
        store_cache.assert_called_once()

    @patch("core.flood_camera_monitoring.presentation.demo_views._store_cache")
    @patch("core.flood_camera_monitoring.presentation.demo_views._cached", return_value=None)
    @patch("core.flood_camera_monitoring.presentation.demo_views.aggregate_predictions")
    @patch("core.flood_camera_monitoring.presentation.demo_views.capture_frames")
    @patch("core.flood_camera_monitoring.presentation.demo_views.get_default_classifier")
    @patch("core.flood_camera_monitoring.presentation.demo_views._client")
    def test_prediction_for_unlabeled_segment_does_not_compare_to_a_scenario(
        self,
        client_factory,
        classifier_factory,
        capture,
        aggregate,
        cached,
        store_cache,
    ):
        client_factory.return_value.get_state.return_value = stream_payload(
            expected_state=None
        )
        classifier_factory.return_value = Mock(_fallback=False)
        capture.return_value = [b"one", b"two", b"three"]
        aggregate.return_value = (
            {
                "strong": True,
                "medium_flag": False,
                "decision_flooded": 85.0,
                "mean_normal": 10.0,
                "mean_medium": 5.0,
                "mean_flooded": 85.0,
                "frames_count": 3,
            },
            [],
        )

        response = self.client.get("/api/flood_monitoring/demo/predict")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["validation"]["expected"])
        self.assertIsNone(response.data["validation"]["match"])
        self.assertEqual(response.data["prediction"]["state"], "flooded")
        self.assertEqual(FloodDetectionRecord.objects.count(), 0)
        store_cache.assert_called_once()

    @patch("core.flood_camera_monitoring.presentation.demo_views._cached", return_value=None)
    @patch("core.flood_camera_monitoring.presentation.demo_views.get_default_classifier")
    @patch("core.flood_camera_monitoring.presentation.demo_views._client")
    def test_fallback_model_is_not_accepted(
        self, client_factory, classifier_factory, cached
    ):
        client_factory.return_value.get_state.return_value = stream_payload()
        classifier_factory.return_value = Mock(_fallback=True)

        response = self.client.get("/api/flood_monitoring/demo/predict")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertTrue(response.data["model"]["fallback"])

    @patch("core.flood_camera_monitoring.presentation.demo_views._client")
    def test_predict_waits_for_a_complete_segment(self, client_factory):
        client_factory.return_value.get_state.return_value = stream_payload(
            with_segment=False
        )

        response = self.client.get("/api/flood_monitoring/demo/predict")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @patch("core.flood_camera_monitoring.presentation.demo_views._cached", return_value=None)
    @patch("core.flood_camera_monitoring.presentation.demo_views.capture_frames")
    @patch("core.flood_camera_monitoring.presentation.demo_views.get_default_classifier")
    @patch("core.flood_camera_monitoring.presentation.demo_views._client")
    def test_prediction_returns_504_only_when_segment_has_no_decodable_frame(
        self,
        client_factory,
        classifier_factory,
        capture,
        _cached,
    ):
        client_factory.return_value.get_state.return_value = stream_payload()
        classifier_factory.return_value = Mock(_fallback=False)
        capture.return_value = []

        response = self.client.get("/api/flood_monitoring/demo/predict")

        self.assertEqual(response.status_code, status.HTTP_504_GATEWAY_TIMEOUT)
        self.assertEqual(
            response.data["detail"],
            "Could not capture frames from demo segment",
        )

    @patch("core.flood_camera_monitoring.presentation.demo_views._store_cache")
    @patch("core.flood_camera_monitoring.presentation.demo_views._cached", return_value=None)
    @patch("core.flood_camera_monitoring.presentation.demo_views.aggregate_predictions")
    @patch("core.flood_camera_monitoring.presentation.demo_views.capture_frames")
    @patch("core.flood_camera_monitoring.presentation.demo_views.get_default_classifier")
    @patch("core.flood_camera_monitoring.presentation.demo_views._client")
    def test_prediction_analyzes_the_exact_requested_segment(
        self,
        client_factory,
        classifier_factory,
        capture,
        aggregate,
        _cached,
        _store_cache,
    ):
        client_factory.return_value.get_state.return_value = stream_payload()
        classifier_factory.return_value = Mock(_fallback=False)
        capture.return_value = [b"one"]
        assessment = prediction_result(
            FloodSeverity.NORMAL,
            90.0,
            normal=90.0,
            medium=5.0,
            flooded=5.0,
        )
        aggregate.return_value = (
            {
                "strong": False,
                "medium_flag": False,
                "decision_flooded": 5.0,
                "mean_normal": 90.0,
                "mean_medium": 5.0,
                "mean_flooded": 5.0,
                "frames_count": 1,
            },
            [assessment],
        )

        response = self.client.get("/api/flood_monitoring/demo/predict?sequence=1")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["segment"]["sequence"], 1)
        self.assertEqual(len(response.data["prediction"]["samples"]), 1)
        capture.assert_called_once_with(
            "http://demo-stream:8088/hls/seg_000000001.ts",
            ANY,
        )

    @patch("core.flood_camera_monitoring.presentation.demo_views._client")
    def test_prediction_rejects_a_future_segment(self, client_factory):
        client_factory.return_value.get_state.return_value = stream_payload()

        response = self.client.get("/api/flood_monitoring/demo/predict?sequence=3")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @patch(
        "core.flood_camera_monitoring.presentation.demo_views._release_prediction_lock"
    )
    @patch(
        "core.flood_camera_monitoring.presentation.demo_views._acquire_prediction_lock",
        return_value="lock-token",
    )
    @patch("core.flood_camera_monitoring.presentation.demo_views._store_cache")
    @patch("core.flood_camera_monitoring.presentation.demo_views._compute_prediction")
    @patch(
        "core.flood_camera_monitoring.presentation.demo_views._model_version",
        return_value="model-v1",
    )
    @patch("core.flood_camera_monitoring.presentation.demo_views._cached")
    @patch("core.flood_camera_monitoring.presentation.demo_views._client")
    def test_prediction_batch_orders_history_and_computes_only_anchor(
        self,
        client_factory,
        cached,
        _model_version,
        compute,
        store_cache,
        acquire_lock,
        release_lock,
    ):
        client_factory.return_value.get_state.return_value = stream_payload()
        history_s2 = cached_prediction(0)
        history_s1 = cached_prediction(1)
        anchor = cached_prediction(2)
        cached.side_effect = [history_s2, history_s1, None]
        compute.return_value = (anchor, None)

        response = self.client.post(
            "/api/flood_monitoring/demo/predictions/batch",
            {
                "session_id": "session-1",
                "anchor_sequence": 2,
                "model_version": "model-v1",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["schema_version"], 4)
        self.assertEqual(response.data["segment_duration_seconds"], 2.0)
        self.assertFalse(response.data["partial"])
        self.assertEqual(
            [
                (
                    item["sequence"],
                    item["offset_segments"],
                    item["nominal_offset_seconds"],
                    item["source"],
                )
                for item in response.data["results"]
            ],
            [(0, 2, -4.0, "cache"), (1, 1, -2.0, "cache"), (2, 0, 0.0, "computed")],
        )
        compute.assert_called_once()
        computed_segment = compute.call_args.args[1]
        self.assertEqual(computed_segment["sequence"], 2)
        self.assertEqual(
            [call.args[0] for call in cached.call_args_list],
            [
                "flood:demo:v2:session-1:0:model-v1",
                "flood:demo:v2:session-1:1:model-v1",
                "flood:demo:v2:session-1:2:model-v1",
            ],
        )
        store_cache.assert_called_once()
        acquire_lock.assert_called_once()
        release_lock.assert_called_once()
        self.assertEqual(FloodDetectionRecord.objects.count(), 0)
        self.assertTrue(
            all(
                item["representative_image"] is None
                for item in response.data["results"]
            )
        )

    @patch("core.flood_camera_monitoring.presentation.demo_views.capture_frames")
    @patch(
        "core.flood_camera_monitoring.presentation.demo_views._model_version",
        return_value="model-v1",
    )
    @patch("core.flood_camera_monitoring.presentation.demo_views._cached")
    @patch("core.flood_camera_monitoring.presentation.demo_views._client")
    def test_prediction_batch_missing_history_is_partial_and_cache_only(
        self,
        client_factory,
        cached,
        _model_version,
        capture,
    ):
        client_factory.return_value.get_state.return_value = stream_payload()
        cached.side_effect = [None, None, cached_prediction(2)]

        response = self.client.post(
            "/api/flood_monitoring/demo/predictions/batch",
            {"session_id": "session-1", "anchor_sequence": 2},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["partial"])
        self.assertEqual(
            [item["status"] for item in response.data["results"]],
            ["missing", "missing", "available"],
        )
        self.assertEqual(response.data["results"][2]["source"], "cache")
        capture.assert_not_called()
        self.assertEqual(FloodDetectionRecord.objects.count(), 0)

    @patch("core.flood_camera_monitoring.presentation.demo_views._compute_prediction")
    @patch(
        "core.flood_camera_monitoring.presentation.demo_views._wait_for_cached_prediction",
        return_value=None,
    )
    @patch(
        "core.flood_camera_monitoring.presentation.demo_views._acquire_prediction_lock",
        return_value=None,
    )
    @patch(
        "core.flood_camera_monitoring.presentation.demo_views._model_version",
        return_value="model-v1",
    )
    @patch("core.flood_camera_monitoring.presentation.demo_views._cached")
    @patch("core.flood_camera_monitoring.presentation.demo_views._client")
    def test_prediction_batch_does_not_compute_when_lock_is_unavailable(
        self,
        client_factory,
        cached,
        _model_version,
        acquire_lock,
        wait_for_cache,
        compute,
    ):
        client_factory.return_value.get_state.return_value = stream_payload()
        cached.side_effect = [None, None, None]

        response = self.client.post(
            "/api/flood_monitoring/demo/predictions/batch",
            {"session_id": "session-1", "anchor_sequence": 2},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["partial"])
        anchor = response.data["results"][2]
        self.assertEqual(anchor["status"], "error")
        self.assertEqual(anchor["error"]["code"], "PREDICTION_BUSY")
        acquire_lock.assert_called_once()
        wait_for_cache.assert_called_once()
        compute.assert_not_called()

    @patch(
        "core.flood_camera_monitoring.presentation.demo_views._model_version",
        return_value="model-v1",
    )
    @patch("core.flood_camera_monitoring.presentation.demo_views._cached")
    @patch("core.flood_camera_monitoring.presentation.demo_views._client")
    def test_prediction_batch_rejects_stale_session_and_model(
        self,
        client_factory,
        cached,
        _model_version,
    ):
        client_factory.return_value.get_state.return_value = stream_payload()

        stale_session = self.client.post(
            "/api/flood_monitoring/demo/predictions/batch",
            {"session_id": "old-session", "anchor_sequence": 2},
            format="json",
        )
        stale_model = self.client.post(
            "/api/flood_monitoring/demo/predictions/batch",
            {
                "session_id": "session-1",
                "anchor_sequence": 2,
                "model_version": "old-model",
            },
            format="json",
        )

        self.assertEqual(stale_session.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            stale_session.data["error"]["code"], "SESSION_MISMATCH"
        )
        self.assertEqual(stale_model.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            stale_model.data["error"]["code"], "MODEL_VERSION_MISMATCH"
        )
        cached.assert_not_called()

    @patch(
        "core.flood_camera_monitoring.presentation.demo_views._model_version",
        return_value="model-v1",
    )
    @patch("core.flood_camera_monitoring.presentation.demo_views._client")
    def test_prediction_batch_rejects_future_anchor_with_stable_code(
        self,
        client_factory,
        _model_version,
    ):
        client_factory.return_value.get_state.return_value = stream_payload()

        response = self.client.post(
            "/api/flood_monitoring/demo/predictions/batch",
            {"session_id": "session-1", "anchor_sequence": 3},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            response.data["error"]["code"], "ANCHOR_SEQUENCE_FUTURE"
        )

    @patch(
        "core.flood_camera_monitoring.presentation.demo_views.get_binary_redis"
    )
    def test_representative_frame_endpoint_returns_jpeg_with_private_headers(
        self, redis_factory
    ):
        pipeline = redis_factory.return_value.pipeline.return_value
        pipeline.execute.return_value = [
            b"\xff\xd8jpeg\xff\xd9",
            b'{"session_id":"session-1"}',
            17,
        ]

        response = self.client.get(
            "/api/flood_monitoring/demo/predictions/frames/"
            "0123456789abcdef0123456789abcdef"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b"\xff\xd8jpeg\xff\xd9")
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(
            response["Cache-Control"],
            "private, max-age=17, no-transform",
        )
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Content-Disposition"], "inline")

    @patch(
        "core.flood_camera_monitoring.presentation.demo_views.get_binary_redis"
    )
    def test_representative_frame_endpoint_returns_404_after_expiration(
        self, redis_factory
    ):
        pipeline = redis_factory.return_value.pipeline.return_value
        pipeline.execute.return_value = [None, None, -2]

        response = self.client.get(
            "/api/flood_monitoring/demo/predictions/frames/"
            "0123456789abcdef0123456789abcdef"
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_settings(DEMO_ENABLED=False)
    def test_representative_frame_endpoint_returns_503_when_demo_is_disabled(self):
        response = self.client.get(
            "/api/flood_monitoring/demo/predictions/frames/"
            "0123456789abcdef0123456789abcdef"
        )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
