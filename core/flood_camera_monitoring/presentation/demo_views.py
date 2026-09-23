from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
import uuid

from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.common.cache import (
    cache_get_json,
    cache_set_json,
    get_binary_redis,
    get_redis,
)
from core.flood_camera_monitoring.services.evaluation import (
    EvalConfig,
    aggregate_predictions,
    capture_frames,
    operational_confidence,
    operational_state,
)
from core.flood_camera_monitoring.infra.demo_stream_client import (
    DemoStreamClient,
    DemoStreamUnavailable,
)
from core.flood_camera_monitoring.infra.torch_flood_classifier import (
    get_default_classifier,
)
from core.flood_camera_monitoring.infra.utils import resolve_checkpoint_path
from core.flood_camera_monitoring.presentation.demo_control_views import (
    DemoStateView as BaseDemoStateView,
    DemoStatusView as BaseDemoStatusView,
)
from core.flood_camera_monitoring.presentation.serializers import (
    DemoPredictionBatchSerializer,
    DemoPredictionQuerySerializer,
)


logger = logging.getLogger(__name__)
MAX_REQUESTED_SEGMENT_LAG = 6
DEMO_PREDICTION_SCHEMA_VERSION = 2
DEMO_PREDICTION_BATCH_SCHEMA_VERSION = 4
DEMO_PREDICTION_LOCK_SECONDS = 30
DEMO_PREDICTION_LOCK_WAIT_SECONDS = 1.0
DEMO_REPRESENTATIVE_FRAME_TTL_SECONDS = 30


@dataclass(frozen=True)
class _PredictionComputation:
    prediction: dict[str, Any] | None
    error: dict[str, Any] | None
    representative_frame: bytes | None = None

    def __iter__(self):
        # Preserve the established two-value unpacking contract.
        yield self.prediction
        yield self.error


def _enabled() -> bool:
    return bool(settings.DEMO_ENABLED)


def _client() -> DemoStreamClient:
    return DemoStreamClient()


def _public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    output = dict(payload)
    output.pop("sources", None)
    source = output.get("source")
    if isinstance(source, dict):
        output["source"] = {
            key: value
            for key, value in source.items()
            if key in {"type", "mode", "status"}
        }
    segment = output.get("segment")
    if isinstance(segment, dict):
        output["segment"] = {
            key: value for key, value in segment.items() if key != "internal_url"
        }
    return output


def _model_version() -> str:
    checkpoint = resolve_checkpoint_path()
    try:
        stat = checkpoint.stat()
        value = f"{checkpoint.name}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        value = f"{checkpoint.name}:missing"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _cached(key: str) -> dict[str, Any] | None:
    try:
        value = cache_get_json(key)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _store_cache(key: str, payload: dict[str, Any]) -> None:
    try:
        cache_set_json(key, payload, ex=int(settings.DEMO_PREDICTION_CACHE_SECONDS))
    except Exception:
        logger.warning("Could not cache demo prediction", exc_info=True)


def _prediction_cache_key(session_id: Any, sequence: Any, version: str) -> str:
    return (
        f"flood:demo:v{DEMO_PREDICTION_SCHEMA_VERSION}:"
        f"{session_id}:{sequence}:{version}"
    )


def _frame_bytes_key(frame_id: str) -> str:
    return f"flood:demo:representative-frame:{frame_id}:jpeg"


def _frame_meta_key(frame_id: str) -> str:
    return f"flood:demo:representative-frame:{frame_id}:meta"


def _frame_identity_key(session_id: str, sequence: int, version: str) -> str:
    identity = hashlib.sha256(
        f"{session_id}\0{sequence}\0{version}".encode("utf-8")
    ).hexdigest()
    return f"flood:demo:representative-frame:identity:{identity}"


def _representative_descriptor(
    session_id: str,
    sequence: int,
    version: str,
) -> dict[str, Any] | None:
    try:
        redis_client = get_binary_redis()
        raw_frame_id = redis_client.get(
            _frame_identity_key(session_id, sequence, version)
        )
        if not raw_frame_id:
            return None
        frame_id = raw_frame_id.decode("ascii")
        raw_meta = redis_client.get(_frame_meta_key(frame_id))
        if not raw_meta or not redis_client.exists(_frame_bytes_key(frame_id)):
            return None
        meta = json.loads(raw_meta.decode("utf-8"))
        if (
            meta.get("session_id") != session_id
            or meta.get("sequence") != sequence
            or meta.get("model_version") != version
        ):
            return None
        return {
            "url": reverse("demo-prediction-frame", kwargs={"frame_id": frame_id}),
            "content_type": "image/jpeg",
            "expires_at": meta["expires_at"],
            "session_id": session_id,
            "sequence": sequence,
            "model_version": version,
        }
    except Exception:
        logger.warning("Could not read representative demo frame", exc_info=True)
        return None


def _store_representative_frame(
    frame: bytes | None,
    session_id: str,
    sequence: int,
    version: str,
) -> dict[str, Any] | None:
    if not frame:
        return None
    frame_id = uuid.uuid4().hex
    expires_at = timezone.now() + timedelta(
        seconds=DEMO_REPRESENTATIVE_FRAME_TTL_SECONDS
    )
    meta = {
        "session_id": session_id,
        "sequence": sequence,
        "model_version": version,
        "expires_at": expires_at.isoformat(),
    }
    try:
        redis_client = get_binary_redis()
        pipeline = redis_client.pipeline(transaction=True)
        pipeline.set(
            _frame_bytes_key(frame_id),
            frame,
            ex=DEMO_REPRESENTATIVE_FRAME_TTL_SECONDS,
        )
        pipeline.set(
            _frame_meta_key(frame_id),
            json.dumps(meta, separators=(",", ":")).encode("utf-8"),
            ex=DEMO_REPRESENTATIVE_FRAME_TTL_SECONDS,
        )
        pipeline.set(
            _frame_identity_key(session_id, sequence, version),
            frame_id.encode("ascii"),
            ex=DEMO_REPRESENTATIVE_FRAME_TTL_SECONDS,
        )
        pipeline.execute()
    except Exception:
        logger.warning("Could not cache representative demo frame", exc_info=True)
        return None
    return {
        "url": reverse("demo-prediction-frame", kwargs={"frame_id": frame_id}),
        "content_type": "image/jpeg",
        "expires_at": meta["expires_at"],
        "session_id": session_id,
        "sequence": sequence,
        "model_version": version,
    }


def _select_representative_frame(
    frames: list[bytes],
    assessments: list[Any],
    state: str,
) -> bytes | None:
    candidates = list(zip(frames, assessments))
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda pair: float(getattr(pair[1].probabilities, state)),
    )[0]


def _acquire_prediction_lock(cache_key: str) -> str | None:
    token = uuid.uuid4().hex
    try:
        acquired = get_redis().set(
            f"{cache_key}:lock",
            token,
            nx=True,
            ex=DEMO_PREDICTION_LOCK_SECONDS,
        )
    except Exception:
        logger.warning("Could not acquire demo prediction lock", exc_info=True)
        return None
    return token if acquired else None


def _release_prediction_lock(cache_key: str, token: str) -> None:
    lock_key = f"{cache_key}:lock"
    try:
        get_redis().eval(
            """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            end
            return 0
            """,
            1,
            lock_key,
            token,
        )
    except Exception:
        logger.warning("Could not release demo prediction lock", exc_info=True)


def _wait_for_cached_prediction(cache_key: str) -> dict[str, Any] | None:
    deadline = time.monotonic() + DEMO_PREDICTION_LOCK_WAIT_SECONDS
    while time.monotonic() < deadline:
        cached = _cached(cache_key)
        if cached is not None:
            return cached
        time.sleep(0.05)
    return None


def _segment_for_sequence(segment: dict[str, Any], sequence: int) -> dict[str, Any]:
    internal_url = str(segment["internal_url"])
    return {
        **segment,
        "sequence": sequence,
        "internal_url": (
            f"{internal_url.rsplit('/', 1)[0]}/seg_{sequence:09d}.ts"
        ),
    }


def _compute_prediction(
    stream_state: dict[str, Any],
    segment: dict[str, Any],
    version: str,
) -> _PredictionComputation:
    classifier = get_default_classifier()
    fallback = bool(getattr(classifier, "_fallback", False))
    if fallback:
        return _PredictionComputation(
            None,
            {
                "code": "MODEL_UNAVAILABLE",
                "detail": "The real flood model is not available",
            },
        )

    cfg = EvalConfig(sample_frames=3, warmup_drops=0, sample_interval_ms=0)
    try:
        frames = capture_frames(str(segment["internal_url"]), cfg)
    except Exception as exc:
        logger.exception("Could not capture demo segment")
        return _PredictionComputation(
            None,
            {
                "code": "SEGMENT_CAPTURE_FAILED",
                "detail": f"Could not capture demo segment: {exc}",
            },
        )
    if not frames:
        return _PredictionComputation(
            None,
            {
                "code": "SEGMENT_HAS_NO_FRAMES",
                "detail": "Could not capture frames from demo segment",
            },
        )

    try:
        summary, assessments = aggregate_predictions(frames, classifier, cfg)
    except Exception:
        logger.exception("Could not classify demo segment")
        return _PredictionComputation(
            None,
            {
                "code": "SEGMENT_CLASSIFICATION_FAILED",
                "detail": "Could not classify demo segment",
            },
        )
    samples = [
        _public_sample(index, assessment)
        for index, assessment in enumerate(assessments)
    ]
    actual = operational_state(summary)
    raw_expected = segment.get("expected_state")
    expected = raw_expected if isinstance(raw_expected, str) else None
    public_segment = {
        key: value for key, value in segment.items() if key != "internal_url"
    }
    return _PredictionComputation(
        {
            "schema_version": DEMO_PREDICTION_SCHEMA_VERSION,
            "session_id": stream_state.get("session_id"),
            "demo_state": stream_state.get("demo_state"),
            "segment": public_segment,
            "prediction": {
                "state": actual,
                "confidence": operational_confidence(summary, actual),
                "probabilities": {
                    "normal": float(summary["mean_normal"]),
                    "medium": float(summary["mean_medium"]),
                    "flooded": float(summary["mean_flooded"]),
                },
                "frames": len(samples),
                "samples": samples,
            },
            "validation": {
                "expected": expected,
                "actual": actual,
                "match": expected == actual if expected is not None else None,
            },
            "model": {"ready": True, "fallback": False, "version": version},
        },
        None,
        _select_representative_frame(frames, assessments, actual),
    )


def _conflict(code: str, detail: str) -> Response:
    return Response(
        {"error": {"code": code, "detail": detail}},
        status=status.HTTP_409_CONFLICT,
    )


def _public_sample(index: int, assessment: Any) -> dict[str, Any]:
    severity = assessment.severity
    state = severity.value if hasattr(severity, "value") else str(severity)
    probabilities = assessment.probabilities
    return {
        "index": index,
        "state": state,
        "confidence": float(assessment.confidence),
        "probabilities": {
            "normal": float(probabilities.normal),
            "medium": float(probabilities.medium),
            "flooded": float(probabilities.flooded),
        },
    }


class DemoStatusView(BaseDemoStatusView):
    """Full-service status preserving the DEMO_ENABLED contract."""

    require_enabled = True


class DemoStateView(BaseDemoStateView):
    """Full-service control preserving the DEMO_ENABLED contract."""

    require_enabled = True


class DemoPredictView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, *args, **kwargs):
        if not _enabled():
            return Response(
                {"detail": "Demo stream is disabled"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            stream_state = _client().get_state()
        except DemoStreamUnavailable as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )

        segment = stream_state.get("segment")
        if not isinstance(segment, dict) or not segment.get("internal_url"):
            return Response(
                {"detail": "No complete demo segment is available yet"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        query = DemoPredictionQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        requested_sequence = query.validated_data.get("sequence")
        if requested_sequence is not None:
            latest_sequence = int(segment["sequence"])
            if requested_sequence > latest_sequence:
                return Response(
                    {"detail": "The requested demo segment is not complete yet"},
                    status=status.HTTP_409_CONFLICT,
                )
            if latest_sequence - requested_sequence > MAX_REQUESTED_SEGMENT_LAG:
                return Response(
                    {"detail": "The requested demo segment is no longer available"},
                    status=status.HTTP_410_GONE,
                )
            segment = _segment_for_sequence(segment, requested_sequence)

        version = _model_version()
        cache_key = _prediction_cache_key(
            stream_state.get("session_id"), segment.get("sequence"), version
        )
        cached = _cached(cache_key)
        if cached is not None:
            return Response(cached)

        payload, error = _compute_prediction(stream_state, segment, version)
        if error is not None:
            response_status = (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if error["code"] == "MODEL_UNAVAILABLE"
                else status.HTTP_504_GATEWAY_TIMEOUT
            )
            body: dict[str, Any] = {"detail": error["detail"]}
            if error["code"] == "MODEL_UNAVAILABLE":
                body["model"] = {
                    "ready": False,
                    "fallback": True,
                    "version": version,
                }
            return Response(
                body,
                status=response_status,
            )
        assert payload is not None
        _store_cache(cache_key, payload)
        return Response(payload)


class DemoPredictionBatchView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        serializer = DemoPredictionBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        requested = serializer.validated_data

        if not _enabled():
            return Response(
                {
                    "error": {
                        "code": "DEMO_UNAVAILABLE",
                        "detail": "Demo stream is disabled",
                    }
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            stream_state = _client().get_state()
        except DemoStreamUnavailable:
            return Response(
                {
                    "error": {
                        "code": "DEMO_UNAVAILABLE",
                        "detail": "Demo stream is unavailable",
                    }
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        segment = stream_state.get("segment")
        if not isinstance(segment, dict) or not segment.get("internal_url"):
            return Response(
                {
                    "error": {
                        "code": "SEGMENT_UNAVAILABLE",
                        "detail": "No complete demo segment is available yet",
                    }
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        session_id = str(stream_state.get("session_id") or "")
        if requested["session_id"] != session_id:
            return _conflict(
                "SESSION_MISMATCH",
                "The requested demo session is no longer current",
            )

        version = _model_version()
        if (
            requested.get("model_version") is not None
            and requested["model_version"] != version
        ):
            return _conflict(
                "MODEL_VERSION_MISMATCH",
                "The requested model version is no longer current",
            )

        anchor = int(requested["anchor_sequence"])
        latest = int(segment["sequence"])
        if anchor > latest:
            return _conflict(
                "ANCHOR_SEQUENCE_FUTURE",
                "The anchor segment is not complete yet",
            )
        if latest - anchor > MAX_REQUESTED_SEGMENT_LAG:
            return _conflict(
                "ANCHOR_SEQUENCE_STALE",
                "The anchor segment is no longer current",
            )

        raw_duration = stream_state.get("scenario", {}).get("segment_seconds")
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            return Response(
                {
                    "error": {
                        "code": "SEGMENT_DURATION_UNAVAILABLE",
                        "detail": "Demo segment duration is unavailable",
                    }
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        results: list[dict[str, Any]] = []
        for offset_segments in (2, 1, 0):
            sequence = anchor - offset_segments
            item = {
                "sequence": sequence,
                "offset_segments": offset_segments,
                "nominal_offset_seconds": -offset_segments * duration,
                "status": "missing",
                "source": None,
                "prediction": None,
                "error": None,
                "representative_image": None,
            }
            if sequence < 0:
                results.append(item)
                continue
            cache_key = _prediction_cache_key(session_id, sequence, version)
            cached = _cached(cache_key)
            if cached is not None:
                item.update(
                    status="available",
                    source="cache",
                    prediction=cached,
                    representative_image=_representative_descriptor(
                        session_id, sequence, version
                    ),
                )
                results.append(item)
                continue
            if offset_segments:
                if latest - sequence > MAX_REQUESTED_SEGMENT_LAG:
                    item["status"] = "gone"
                results.append(item)
                continue

            token = _acquire_prediction_lock(cache_key)
            if token is None:
                cached = _wait_for_cached_prediction(cache_key)
                if cached is not None:
                    item.update(
                        status="available",
                        source="cache",
                        prediction=cached,
                        representative_image=_representative_descriptor(
                            session_id, sequence, version
                        ),
                    )
                else:
                    item.update(
                        status="error",
                        error={
                            "code": "PREDICTION_BUSY",
                            "detail": "Prediction is already being computed",
                        },
                    )
                results.append(item)
                continue
            try:
                anchor_segment = _segment_for_sequence(segment, anchor)
                computation = _compute_prediction(
                    stream_state, anchor_segment, version
                )
                prediction, error = computation
                if error is not None:
                    if error["code"] == "MODEL_UNAVAILABLE":
                        return Response(
                            {
                                "error": error,
                                "model": {
                                    "ready": False,
                                    "fallback": True,
                                    "version": version,
                                },
                            },
                            status=status.HTTP_503_SERVICE_UNAVAILABLE,
                        )
                    public_error = dict(error)
                    if public_error["code"] == "SEGMENT_CAPTURE_FAILED":
                        public_error["detail"] = "Could not capture demo segment"
                    item.update(status="error", error=public_error)
                else:
                    assert prediction is not None
                    _store_cache(cache_key, prediction)
                    item.update(
                        status="available",
                        source="computed",
                        prediction=prediction,
                        representative_image=_store_representative_frame(
                            getattr(computation, "representative_frame", None),
                            session_id,
                            anchor,
                            version,
                        ),
                    )
            finally:
                _release_prediction_lock(cache_key, token)
            results.append(item)

        return Response(
            {
                "schema_version": DEMO_PREDICTION_BATCH_SCHEMA_VERSION,
                "session_id": session_id,
                "anchor_sequence": anchor,
                "segment_duration_seconds": duration,
                "model": {
                    "ready": True,
                    "fallback": False,
                    "version": version,
                },
                "partial": any(item["status"] != "available" for item in results),
                "results": results,
            }
        )


class DemoPredictionFrameView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, frame_id: str, *args, **kwargs):
        if not _enabled():
            return Response(
                {"detail": "Demo stream is disabled"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if len(frame_id) != 32 or any(
            character not in "0123456789abcdef" for character in frame_id
        ):
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            redis_client = get_binary_redis()
            pipeline = redis_client.pipeline(transaction=True)
            pipeline.get(_frame_bytes_key(frame_id))
            pipeline.get(_frame_meta_key(frame_id))
            pipeline.ttl(_frame_bytes_key(frame_id))
            frame, raw_meta, ttl = pipeline.execute()
            if not frame or not raw_meta or int(ttl) < 0:
                return Response(status=status.HTTP_404_NOT_FOUND)
            json.loads(raw_meta.decode("utf-8"))
        except Exception:
            logger.warning("Could not read representative demo frame", exc_info=True)
            return Response(status=status.HTTP_404_NOT_FOUND)

        response = HttpResponse(frame, content_type="image/jpeg")
        response["Cache-Control"] = (
            f"private, max-age={min(int(ttl), DEMO_REPRESENTATIVE_FRAME_TTL_SECONDS)}, "
            "no-transform"
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Disposition"] = "inline"
        return response
