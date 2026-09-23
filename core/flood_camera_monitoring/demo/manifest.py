from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ALLOWED_LABELS = {"normal", "medium", "flooded"}
ALLOWED_STATES = {"auto", "normal", "flooded"}
ENVIRONMENT_VARIABLE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
VideoResolver = Callable[[str], Path]


class DemoManifestError(ValueError):
    """Raised when a demo scenario cannot be safely executed."""


@dataclass(frozen=True)
class DemoPhase:
    name: str
    file_path: Path
    label: str | None
    duration_seconds: int

    @property
    def state(self) -> str:
        return self.label or "auto"


@dataclass(frozen=True)
class DemoScenario:
    scenario_id: str
    phases: tuple[DemoPhase, ...]
    segment_seconds: int = 2

    @property
    def available_states(self) -> tuple[str, ...]:
        return tuple(
            state
            for state in ("auto", "normal", "flooded")
            if self.phase_for_state(state, required=False)
        )

    def phase_for_state(self, state: str, *, required: bool = True) -> DemoPhase | None:
        if state not in ALLOWED_STATES:
            raise DemoManifestError(f"Unsupported demo state: {state}")
        selected = tuple(phase for phase in self.phases if phase.state == state)
        if len(selected) > 1:
            raise DemoManifestError(
                f"Scenario must define exactly one video for state '{state}'"
            )
        if not selected:
            if required:
                raise DemoManifestError(f"Scenario has no video for state '{state}'")
            return None
        return selected[0]

    def phases_for_state(self, state: str) -> tuple[DemoPhase, ...]:
        phase = self.phase_for_state(state)
        return (phase,) if phase is not None else ()

    def phase_for_sequence(self, state: str, sequence: int) -> DemoPhase:
        if sequence < 0:
            raise DemoManifestError("Segment sequence cannot be negative")

        phases = self.phases_for_state(state)
        timeline: list[DemoPhase] = []
        for phase in phases:
            timeline.extend([phase] * (phase.duration_seconds // self.segment_seconds))
        if not timeline:
            raise DemoManifestError("Scenario timeline is empty")
        return timeline[sequence % len(timeline)]


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DemoManifestError(f"'{field}' must be an object")
    return value


def _safe_asset_path(asset_root: Path, raw_path: Any) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise DemoManifestError("Every phase must define a non-empty 'file'")
    if "\n" in raw_path or "\r" in raw_path or "'" in raw_path:
        raise DemoManifestError("Phase file contains unsupported characters")
    root = asset_root.resolve()
    candidate = (root / raw_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise DemoManifestError("Phase file must stay inside the demo assets directory")
    if not candidate.is_file():
        raise DemoManifestError(f"Demo video not found: {raw_path}")
    return candidate


def _resolve_phase_video(
    manifest_path: Path,
    phase: dict[str, Any],
    video_resolver: VideoResolver | None,
    require_uploader: bool = False,
    override_attachment_key: str | None = None,
) -> Path:
    if override_attachment_key is not None:
        if video_resolver is None:
            raise DemoManifestError(
                "Uploader video overrides require a configured video resolver"
            )
        if not override_attachment_key.strip():
            raise DemoManifestError("Video attachment key override cannot be empty")
        return video_resolver(override_attachment_key.strip())

    file_value = phase.get("file")
    attachment_key = phase.get("video_attachment_key")
    attachment_key_env = phase.get("video_attachment_key_env")
    configured_sources = sum(
        value is not None
        for value in (file_value, attachment_key, attachment_key_env)
    )
    if configured_sources != 1:
        raise DemoManifestError(
            "Every phase must define exactly one of 'file', "
            "'video_attachment_key', or 'video_attachment_key_env'"
        )

    if file_value is not None:
        if require_uploader:
            raise DemoManifestError(
                "A demonstração operacional aceita somente vídeos do uploader; remova 'file'"
            )
        return _safe_asset_path(manifest_path.parent, file_value)

    if video_resolver is None:
        raise DemoManifestError(
            "Uploader video sources require a configured video resolver"
        )

    if attachment_key_env is not None:
        valid_environment_name = (
            isinstance(attachment_key_env, str)
            and ENVIRONMENT_VARIABLE_RE.fullmatch(attachment_key_env)
        )
        if not valid_environment_name:
            raise DemoManifestError(
                "'video_attachment_key_env' must be a valid environment variable name"
            )
        attachment_key = os.getenv(attachment_key_env)
        if not attachment_key:
            raise DemoManifestError(
                f"Environment variable {attachment_key_env} is not configured"
            )

    if not isinstance(attachment_key, str) or not attachment_key.strip():
        raise DemoManifestError("Video attachment key must be a non-empty string")
    return video_resolver(attachment_key.strip())


def load_scenario(
    path: str | Path,
    *,
    video_resolver: VideoResolver | None = None,
    require_uploader: bool = False,
    state_video_keys: dict[str, str] | None = None,
) -> DemoScenario:
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise DemoManifestError(f"Scenario manifest not found: {manifest_path}")

    try:
        raw = _require_mapping(
            json.loads(manifest_path.read_text(encoding="utf-8")), "scenario"
        )
    except json.JSONDecodeError as exc:
        raise DemoManifestError(f"Invalid scenario JSON: {exc}") from exc

    scenario_id = raw.get("scenario_id")
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        raise DemoManifestError("'scenario_id' must be a non-empty string")

    segment_seconds = raw.get("segment_seconds", 2)
    if isinstance(segment_seconds, bool) or not isinstance(segment_seconds, int):
        raise DemoManifestError("'segment_seconds' must be an integer")
    if segment_seconds <= 0:
        raise DemoManifestError("'segment_seconds' must be greater than zero")

    raw_phases = raw.get("phases")
    if not isinstance(raw_phases, list) or not raw_phases:
        raise DemoManifestError("'phases' must be a non-empty list")

    phases: list[DemoPhase] = []
    for index, value in enumerate(raw_phases):
        phase = _require_mapping(value, f"phases[{index}]")
        name = phase.get("name")
        label = phase.get("label")
        duration = phase.get("duration_seconds")
        if not isinstance(name, str) or not name.strip():
            raise DemoManifestError(f"phases[{index}].name must be non-empty")
        if label is not None and label not in ALLOWED_LABELS:
            raise DemoManifestError(
                f"phases[{index}].label must be null or one of {sorted(ALLOWED_LABELS)}"
            )
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            raise DemoManifestError(
                f"phases[{index}].duration_seconds must be a positive integer"
            )
        if duration % segment_seconds != 0:
            raise DemoManifestError(
                f"phases[{index}].duration_seconds must be divisible by segment_seconds"
            )
        phases.append(
            DemoPhase(
                name=name.strip(),
                file_path=_resolve_phase_video(
                    manifest_path,
                    phase,
                    video_resolver,
                    require_uploader=require_uploader,
                    override_attachment_key=(state_video_keys or {}).get(
                        label or "auto"
                    ),
                ),
                label=label,
                duration_seconds=duration,
            )
        )

    scenario = DemoScenario(
        scenario_id=scenario_id.strip(),
        phases=tuple(phases),
        segment_seconds=segment_seconds,
    )
    for state in ("auto", "normal", "flooded"):
        scenario.phase_for_state(state)
    return scenario


def scenario_as_dict(scenario: DemoScenario) -> dict[str, Any]:
    return {
        "scenario_id": scenario.scenario_id,
        "segment_seconds": scenario.segment_seconds,
        "available_states": list(scenario.available_states),
        "phases": [
            {
                "name": phase.name,
                "label": phase.label,
                "duration_seconds": phase.duration_seconds,
            }
            for phase in scenario.phases
        ],
    }
