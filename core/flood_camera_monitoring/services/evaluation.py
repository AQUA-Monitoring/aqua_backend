from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import os
from typing import List, Tuple, Dict, Any

from core.flood_camera_monitoring.infra.opencv_stream import (
    OpenCVVideoStream,
)
from core.flood_camera_monitoring.services.stream_prediction import (
    PredictionResult,
)


@dataclass
class EvalConfig:
    sample_frames: int = int(os.getenv("FLOOD_SAMPLE_FRAMES", "3"))
    sample_interval_ms: int = int(os.getenv("FLOOD_SAMPLE_INTERVAL_MS", "150"))
    warmup_drops: int = int(os.getenv("FLOOD_WARMUP_DROPS", "2"))
    strong_min: float = float(os.getenv("FLOOD_STRONG_MIN", "60.0"))
    medium_min: float = float(os.getenv("FLOOD_MEDIUM_MIN", "25.0"))
    medium_max: float = float(os.getenv("FLOOD_MEDIUM_MAX", "60.0"))
    trend_min_delta: float = float(os.getenv("FLOOD_TREND_MIN_DELTA", "10.0"))
    min_medium_frames: int = int(os.getenv("FLOOD_MIN_MEDIUM_FRAMES", "2"))


def operational_state(summary: Dict[str, Any]) -> str:
    """Return the project's operational three-level state for an evaluation.

    The demo must use the same thresholds as camera evaluation: a strong
    flooded signal wins, then the early-warning medium condition, otherwise
    the result is normal.
    """
    if bool(summary.get("strong")):
        return "flooded"
    if bool(summary.get("medium_flag")):
        return "medium"
    return "normal"


def operational_confidence(summary: Dict[str, Any], state: str) -> float:
    """Return the confidence associated with the selected operational state."""
    if state == "flooded":
        value = summary.get("decision_flooded", summary.get("mean_flooded", 0.0))
    elif state == "medium":
        value = summary.get("mean_medium", summary.get("mean_flooded", 0.0))
    else:
        value = summary.get("mean_normal", 0.0)
    return round(max(0.0, min(100.0, float(value))), 2)


def capture_frames(stream_url: str, cfg: EvalConfig) -> list[bytes]:
    """Decode a complete finite source and retain its final frames in order.

    Demo segment URLs identify complete media files. A bounded deque keeps the
    memory cost independent of the segment duration while preserving the last
    decodable frames in chronological order.
    """
    stream = OpenCVVideoStream(stream_url)
    frames: deque[bytes] = deque(maxlen=max(1, int(cfg.sample_frames)))
    try:
        for _ in range(max(0, int(cfg.warmup_drops))):
            _ = stream.grab()
        while True:
            img_bytes = stream.grab()
            if img_bytes is None:
                break
            frames.append(img_bytes)
    finally:
        stream.close()
    return list(frames)


def aggregate_predictions(
    frames: List[bytes],
    classifier,
    cfg: EvalConfig,
) -> Tuple[Dict[str, Any], List[PredictionResult]]:
    """Run classifier on frames and compute aggregated metrics and flags.

    Returns: (summary_dict, assessments)
    """
    assessments: list[PredictionResult] = []
    best_idx = 0
    best_flooded = -1.0
    flooded_series: list[float] = []
    for idx, fb in enumerate(frames):
        a = classifier.predict(fb)
        assessments.append(a)
        flooded = float(a.probabilities.flooded)
        if flooded > best_flooded:
            best_flooded = flooded
            best_idx = idx
        flooded_series.append(flooded)

    mean_normal = sum(float(a.probabilities.normal) for a in assessments) / len(
        assessments
    )
    mean_flooded = sum(float(a.probabilities.flooded) for a in assessments) / len(
        assessments
    )
    try:
        mean_medium = sum(float(a.probabilities.medium) for a in assessments) / len(
            assessments
        )
    except Exception:
        mean_medium = 0.0

    # Normalize to 100%
    total = mean_normal + mean_flooded + mean_medium
    if total > 0:
        mean_normal = (mean_normal / total) * 100.0
        mean_flooded = (mean_flooded / total) * 100.0
        mean_medium = 100.0 - (mean_normal + mean_flooded)

    decision_flooded = max(best_flooded, mean_flooded)
    chosen = assessments[best_idx]
    chosen_bytes = frames[best_idx]
    chosen_conf = float(
        mean_flooded
        if decision_flooded == mean_flooded
        else max(chosen.probabilities.flooded, chosen.probabilities.normal)
    )

    strong = decision_flooded >= float(cfg.strong_min)
    rising_trend = False
    if len(flooded_series) >= 2:
        rising_trend = (flooded_series[-1] - flooded_series[0]) >= float(
            cfg.trend_min_delta
        )
    medium_band = float(cfg.medium_min) <= mean_flooded < float(cfg.medium_max)
    medium_frames = sum(
        1 for v in flooded_series if float(cfg.medium_min) <= v < float(cfg.strong_min)
    )
    medium_flag = (not strong) and (
        medium_band
        or medium_frames >= int(cfg.min_medium_frames)
        or (rising_trend and flooded_series[-1] >= float(cfg.medium_min))
    )

    summary = {
        "mean_normal": float(mean_normal),
        "mean_flooded": float(mean_flooded),
        "mean_medium": float(mean_medium),
        "decision_flooded": float(decision_flooded),
        "best_flooded": float(best_flooded),
        "chosen_confidence": float(chosen_conf),
        "strong": bool(strong),
        "medium_flag": bool(medium_flag),
        "medium_band": bool(medium_band),
        "medium_frames": int(medium_frames),
        "trend": {"series": flooded_series, "rising": bool(rising_trend)},
        "chosen_bytes": chosen_bytes,
        "frames_count": len(assessments),
    }

    return summary, assessments
