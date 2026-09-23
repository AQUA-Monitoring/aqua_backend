from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import time
from typing import Any, Callable

from django.core.files.base import ContentFile
from django.db import transaction

from core.flood_camera_monitoring.infra.opencv_stream import (
    OpenCVVideoStream,
)
from core.flood_camera_monitoring.infra.torch_classifier import (
    TorchFloodClassifier,
)
from core.flood_camera_monitoring.services.operational_snapshot import (
    begin_analysis,
    begin_capture,
    get_or_create_operational_snapshot,
    mark_available,
    mark_error,
    mark_model_unavailable,
    mark_no_frame,
    mark_stream_online,
    snapshot_prediction_payload,
)
from core.flood_camera_monitoring.services.evaluation import (
    EvalConfig,
    aggregate_predictions,
)
from core.flood_camera_monitoring.services.model_artifact import (
    ModelArtifactInfo,
    inspect_model_artifact,
)
from core.flood_camera_monitoring.services.operational_alerts import (
    create_or_update_alert_for_detection,
)
from core.flood_camera_monitoring.infra.models import (
    Camera,
    CameraOperationalSnapshot,
    FloodDetectionRecord,
)


logger = logging.getLogger(__name__)


@dataclass
class AnalyzeAllCamerasService:
    """Atualiza snapshots e persiste somente indicações operacionais válidas."""

    min_confidence: float = float(os.getenv("FLOOD_MIN_CONFIDENCE", "10.0"))
    medium_min_confidence: float = float(
        os.getenv("FLOOD_MEDIUM_MIN_CONFIDENCE", "80.0")
    )
    sample_frames: int = int(os.getenv("FLOOD_SAMPLE_FRAMES", "3"))
    sample_interval_ms: int = int(os.getenv("FLOOD_SAMPLE_INTERVAL_MS", "150"))
    warmup_drops: int = int(os.getenv("FLOOD_WARMUP_DROPS", "2"))
    strong_min: float = float(os.getenv("FLOOD_STRONG_MIN", "60.0"))
    medium_min: float = float(os.getenv("FLOOD_MEDIUM_MIN", "25.0"))
    medium_max: float = float(os.getenv("FLOOD_MEDIUM_MAX", "60.0"))
    trend_min_delta: float = float(os.getenv("FLOOD_TREND_MIN_DELTA", "10.0"))
    min_medium_frames: int = int(os.getenv("FLOOD_MIN_MEDIUM_FRAMES", "2"))
    stream_factory: Callable[[str], Any] = OpenCVVideoStream
    classifier_factory: Callable[[str], Any] = TorchFloodClassifier
    artifact_inspector: Callable[[], ModelArtifactInfo] = inspect_model_artifact
    camera_id: Any = None
    capture_observer: Any = None
    prediction_observer: Any = None
    drain_between_samples: bool = False
    persist_legacy_images: bool = True

    def run(self) -> int:
        _, saved = self.run_and_collect()
        return saved

    def run_and_collect(self) -> tuple[list[dict[str, Any]], int]:
        data: list[dict[str, Any]] = []
        saved = 0
        classifier = None
        classifier_resolved = False
        classifier_error_code: str | None = None
        artifact: ModelArtifactInfo | None = None

        cameras = Camera.objects.filter(status=Camera.CameraStatus.ACTIVE)
        if self.camera_id is not None:
            cameras = cameras.filter(pk=self.camera_id)
        cameras = cameras.iterator()
        for camera in cameras:
            stream_url = getattr(camera, "video_hls", None)
            if self._is_demo_camera(camera, stream_url):
                logger.info(
                    "Skipping demo camera id=%s in operational analysis.", camera.id
                )
                continue

            snapshot = get_or_create_operational_snapshot(camera)
            begin_capture(snapshot)

            if not stream_url:
                mark_no_frame(snapshot, error_code="STREAM_NOT_CONFIGURED")
                data.append(snapshot_prediction_payload(camera, snapshot))
                continue

            frames, capture_error = self._capture_frames(str(stream_url))
            if self.capture_observer is not None:
                self.capture_observer(camera, frames)
            if capture_error:
                mark_error(
                    snapshot,
                    error_code="STREAM_CAPTURE_ERROR",
                    stream_unavailable=True,
                )
                data.append(snapshot_prediction_payload(camera, snapshot))
                continue
            if not frames:
                mark_no_frame(snapshot)
                data.append(snapshot_prediction_payload(camera, snapshot))
                continue

            mark_stream_online(snapshot)
            begin_analysis(snapshot, frames=len(frames))

            if artifact is None:
                artifact = self.artifact_inspector()
            if not artifact.available:
                mark_model_unavailable(
                    snapshot,
                    fallback=False,
                    model_version=None,
                    error_code=artifact.error_code or "MODEL_UNAVAILABLE",
                )
                data.append(snapshot_prediction_payload(camera, snapshot))
                continue

            if not classifier_resolved:
                classifier_resolved = True
                try:
                    classifier = self.classifier_factory(str(artifact.path))
                except Exception:
                    classifier_error_code = "MODEL_LOAD_FAILED"
                    logger.exception("Could not initialize the operational classifier")

            if classifier is None:
                mark_model_unavailable(
                    snapshot,
                    fallback=False,
                    model_version=artifact.version,
                    error_code=classifier_error_code or "MODEL_UNAVAILABLE",
                )
                data.append(snapshot_prediction_payload(camera, snapshot))
                continue

            if bool(getattr(classifier, "_fallback", False)):
                mark_model_unavailable(
                    snapshot,
                    fallback=True,
                    model_version=artifact.version,
                    error_code="MODEL_FALLBACK",
                )
                data.append(snapshot_prediction_payload(camera, snapshot))
                continue

            try:
                summary, assessments = aggregate_predictions(frames, classifier, self._eval_config())
                if self.prediction_observer is not None:
                    self.prediction_observer(camera, summary, assessments)
            except Exception:
                logger.exception("Inference failed for camera id=%s", camera.id)
                mark_error(
                    snapshot,
                    error_code="INFERENCE_ERROR",
                    model_status=CameraOperationalSnapshot.ModelStatus.READY,
                    model_version=artifact.version,
                )
                data.append(snapshot_prediction_payload(camera, snapshot))
                continue

            classification = self._classification(summary)
            confidence = self._classification_confidence(summary, classification)
            mark_available(
                snapshot,
                classification=classification,
                prob_normal=float(summary["mean_normal"]),
                prob_medium=float(summary["mean_medium"]),
                prob_flooded=float(summary["mean_flooded"]),
                confidence=confidence,
                frames=int(summary["frames_count"]),
                model_version=artifact.version or "unknown",
            )

            if classification in {
                CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION,
                CameraOperationalSnapshot.CameraClassification.INTERMEDIATE_INDICATION,
            }:
                if self._persist_detection(
                    camera, snapshot, frames if self.persist_legacy_images else [],
                    summary if self.persist_legacy_images else {**summary, 'chosen_bytes': None}, classification
                ):
                    saved += 1

            data.append(snapshot_prediction_payload(camera, snapshot))

        logger.info(
            "Operational camera analysis finished: snapshots=%s persisted=%s",
            len(data),
            saved,
        )
        return data, saved

    def _capture_frames(self, stream_url: str) -> tuple[list[bytes], bool]:
        stream = None
        frames: list[bytes] = []
        try:
            stream = self.stream_factory(stream_url)
            for _ in range(max(0, int(self.warmup_drops))):
                stream.grab()
            attempts = max(1, int(self.sample_frames))
            for index in range(attempts):
                frame = stream.grab()
                if frame:
                    frames.append(frame)
                if index < attempts - 1 and self.sample_interval_ms > 0:
                    if self.drain_between_samples:
                        deadline = time.monotonic() + self.sample_interval_ms / 1000.0
                        while time.monotonic() < deadline:
                            if stream.grab() is None:
                                break
                    else:
                        time.sleep(self.sample_interval_ms / 1000.0)
        except Exception:
            logger.exception("Could not capture frames from an operational camera")
            return [], True
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    logger.warning("Could not close an operational stream", exc_info=True)
        return frames, False

    def _eval_config(self) -> EvalConfig:
        return EvalConfig(
            sample_frames=self.sample_frames,
            sample_interval_ms=self.sample_interval_ms,
            warmup_drops=self.warmup_drops,
            strong_min=self.strong_min,
            medium_min=self.medium_min,
            medium_max=self.medium_max,
            trend_min_delta=self.trend_min_delta,
            min_medium_frames=self.min_medium_frames,
        )

    @staticmethod
    def _classification(summary: dict[str, Any]) -> str:
        if bool(summary.get("strong")):
            return CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION
        if bool(summary.get("medium_flag")):
            return (
                CameraOperationalSnapshot.CameraClassification.INTERMEDIATE_INDICATION
            )
        return CameraOperationalSnapshot.CameraClassification.NO_INDICATION

    @staticmethod
    def _classification_confidence(
        summary: dict[str, Any], classification: str
    ) -> float:
        if (
            classification
            == CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION
        ):
            value = summary.get("decision_flooded", summary.get("mean_flooded", 0.0))
        elif (
            classification
            == CameraOperationalSnapshot.CameraClassification.INTERMEDIATE_INDICATION
        ):
            value = max(
                float(summary.get("mean_medium", 0.0)),
                float(summary.get("mean_flooded", 0.0)),
            )
        else:
            value = summary.get("mean_normal", 0.0)
        return max(0.0, min(100.0, float(value)))

    @staticmethod
    def _is_demo_camera(camera, stream_url: Any) -> bool:
        if isinstance(stream_url, str) and stream_url.startswith("loop:"):
            return True
        for attr in ("source_scope", "source_kind"):
            if str(getattr(camera, attr, "")).upper() == "DEMO":
                return True
        return False

    @staticmethod
    def _persist_detection(
        camera,
        snapshot: CameraOperationalSnapshot,
        frames: list[bytes],
        summary: dict[str, Any],
        classification: str,
    ) -> bool:
        is_flooded = (
            classification
            == CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION
        )
        is_medium = (
            classification
            == CameraOperationalSnapshot.CameraClassification.INTERMEDIATE_INDICATION
        )
        confidence = float(summary.get("decision_flooded", 0.0))
        chosen_bytes = summary.get("chosen_bytes") or (frames[0] if frames else None)
        values = {
            "camera": camera,
            "is_flooded": is_flooded,
            "medium": is_medium,
            "confidence": confidence,
            "prob_normal": float(summary["mean_normal"]),
            "prob_flooded": float(summary["mean_flooded"]),
            "prob_medium": float(summary["mean_medium"]),
        }
        try:
            with transaction.atomic():
                detection = FloodDetectionRecord.objects.create(
                    **values,
                    image=(
                        ContentFile(
                            chosen_bytes,
                            name=f"{camera.id}-{int(time.time())}.jpg",
                        )
                        if chosen_bytes
                        else None
                    ),
                )
                if is_flooded:
                    create_or_update_alert_for_detection(detection, snapshot)
            return True
        except Exception:
            logger.warning(
                "Could not persist detection image for camera id=%s; retrying without image",
                camera.id,
                exc_info=True,
            )
        try:
            with transaction.atomic():
                detection = FloodDetectionRecord.objects.create(**values, image=None)
                if is_flooded:
                    create_or_update_alert_for_detection(detection, snapshot)
            return True
        except Exception:
            logger.exception("Could not persist detection for camera id=%s", camera.id)
            return False

    @staticmethod
    def _format_table(headers, rows, max_widths=None) -> str:
        """Compatibilidade com consumidores antigos do formatador de logs."""
        max_widths = max_widths or {}
        headers = list(headers or [])
        if not headers:
            return ""
        widths = [len(str(header)) for header in headers]
        normalized_rows = []
        for row in rows:
            normalized = []
            for index, header in enumerate(headers):
                value = str(row[index]) if index < len(row) else ""
                limit = max_widths.get(header)
                if limit and len(value) > limit:
                    value = value[: max(0, limit - 1)] + "…"
                normalized.append(value)
                widths[index] = max(widths[index], len(value))
            normalized_rows.append(normalized)
        rule = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
        lines = [rule]
        lines.append(
            "|"
            + "|".join(
                f" {str(value):<{widths[index]}} "
                for index, value in enumerate(headers)
            )
            + "|"
        )
        lines.append(rule)
        for row in normalized_rows:
            lines.append(
                "|"
                + "|".join(
                    f" {value:<{widths[index]}} "
                    for index, value in enumerate(row)
                )
                + "|"
            )
        lines.append(rule)
        return "\n".join(lines)
