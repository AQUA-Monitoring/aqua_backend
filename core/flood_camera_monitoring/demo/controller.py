from __future__ import annotations

import logging
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from core.flood_camera_monitoring.demo.manifest import (
    ALLOWED_STATES,
    DemoPhase,
    DemoScenario,
    scenario_as_dict,
)


SEGMENT_RE = re.compile(r"^seg_(\d+)\.ts$")


class DemoStreamError(RuntimeError):
    pass


class DemoStreamController:
    """Owns normalized fixtures and the long-running FFmpeg HLS process."""

    def __init__(
        self,
        scenario: DemoScenario,
        work_dir: str | Path,
        *,
        public_hls_url: str,
        internal_base_url: str,
        source_type: str = "scenario",
        video_resolver: Callable[[str], Path] | None = None,
        playlist_timeout_seconds: float = 10.0,
    ) -> None:
        self.scenario = scenario
        self.work_dir = Path(work_dir)
        self.normalized_dir = self.work_dir / "normalized"
        self.sessions_dir = self.work_dir / "sessions"
        self.hls_dir = self.sessions_dir / "uninitialized"
        self.public_hls_url = public_hls_url
        self.internal_base_url = internal_base_url.rstrip("/")
        self.source_type = source_type
        self.video_resolver = video_resolver
        self.playlist_timeout_seconds = playlist_timeout_seconds
        self.logger = logging.getLogger(__name__)
        self._lock = threading.RLock()
        self._prepare_lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle = None
        self._normalized: dict[str, Path] = {}
        self._sources: dict[str, dict[str, Any]] = {}
        self.state = "auto"
        self.session_id = ""

    def initialize(self, initial_state: str = "auto") -> dict[str, Any]:
        self._require_binary("ffmpeg")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.normalized_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        for phase in self.scenario.phases:
            state = phase.state
            version = uuid.uuid4().hex
            output = self.normalized_dir / state / version / "source.mp4"
            output.parent.mkdir(parents=True, exist_ok=True)
            self._normalize_phase(phase, output)
            self._normalized[state] = output
            self._sources[state] = {
                "mode": state,
                "status": "ready",
                "version": version,
                "attachment_key": None,
            }
        return self.set_state(initial_state)

    @staticmethod
    def _require_binary(name: str) -> None:
        if shutil.which(name) is None:
            raise DemoStreamError(f"Required executable is not available: {name}")

    def _normalize_phase(self, phase: DemoPhase, output: Path) -> None:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(phase.file_path),
            "-t",
            str(phase.duration_seconds),
            "-an",
            "-vf",
            "scale=1280:720:force_original_aspect_ratio=decrease,"
            "pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=10",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(self.scenario.segment_seconds * 10),
            "-keyint_min",
            str(self.scenario.segment_seconds * 10),
            "-sc_threshold",
            "0",
            str(output),
        ]
        self._run(command, f"Could not normalize phase '{phase.name}'")

    @staticmethod
    def _run(command: list[str], error_message: str) -> None:
        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode("utf-8", errors="replace").strip()
            raise DemoStreamError(f"{error_message}: {detail}") from exc

    def set_state(self, state: str) -> dict[str, Any]:
        if state not in ALLOWED_STATES:
            raise DemoStreamError(f"Unsupported demo state: {state}")
        with self._lock:
            self.scenario.phase_for_state(state)
            source = self._normalized.get(state)
            if source is None:
                raise DemoStreamError(f"Demo source for state '{state}' is not ready")
            self._promote(state, source)
            return self.snapshot()

    def _spawn_hls(self, source: Path, hls_dir: Path):
        hls_dir.mkdir(parents=True, exist_ok=False)
        playlist = hls_dir / "playlist.m3u8"
        segment_pattern = hls_dir / "seg_%09d.ts"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-re",
            "-stream_loop",
            "-1",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            str(self.scenario.segment_seconds),
            "-hls_list_size",
            "8",
            "-hls_flags",
            "delete_segments+append_list+independent_segments+program_date_time",
            "-hls_segment_filename",
            str(segment_pattern),
            str(playlist),
        ]
        log_handle = (hls_dir / "ffmpeg.log").open("ab")
        try:
            process = subprocess.Popen(
                command,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )
        except Exception:
            log_handle.close()
            raise
        return process, log_handle

    def _wait_until_ready(self, process, playlist: Path) -> None:
        deadline = time.monotonic() + self.playlist_timeout_seconds
        while time.monotonic() < deadline:
            if playlist.is_file() and playlist.stat().st_size > 0:
                return
            if process.poll() is not None:
                raise DemoStreamError(
                    "Candidate demo stream stopped before playlist was ready"
                )
            time.sleep(0.05)
        raise DemoStreamError("Candidate demo stream playlist was not ready in time")

    @staticmethod
    def _stop_candidate(process, log_handle) -> None:
        if process is not None and process.poll() is None:
            try:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if log_handle is not None:
            log_handle.close()

    def _promote(self, state: str, source: Path) -> None:
        session_id = str(uuid.uuid4())
        candidate_dir = self.sessions_dir / session_id
        process = log_handle = None
        try:
            process, log_handle = self._spawn_hls(source, candidate_dir)
            self._wait_until_ready(process, candidate_dir / "playlist.m3u8")
        except Exception:
            self._stop_candidate(process, log_handle)
            shutil.rmtree(candidate_dir, ignore_errors=True)
            raise

        old_process, old_log = self._process, self._log_handle
        self._process, self._log_handle = process, log_handle
        self.hls_dir = candidate_dir
        self.state = state
        self.session_id = session_id
        self._stop_candidate(old_process, old_log)

    def prepare_source(self, state: str, attachment_key: str) -> dict[str, Any]:
        if state not in ALLOWED_STATES:
            raise DemoStreamError(f"Unsupported demo state: {state}")
        if self.video_resolver is None:
            raise DemoStreamError("Demo video resolver is not configured")
        version = uuid.uuid4().hex
        source_status = {
            "mode": state,
            "status": "preparing",
            "version": version,
            "attachment_key": attachment_key,
        }
        with self._lock:
            self._sources[state] = source_status
        output = self.normalized_dir / state / version / "source.mp4"
        output.parent.mkdir(parents=True, exist_ok=False)
        try:
            with self._prepare_lock:
                source_path = self.video_resolver(attachment_key)
                template = self.scenario.phase_for_state(state)
                assert template is not None
                phase = DemoPhase(
                    template.name,
                    source_path,
                    template.label,
                    template.duration_seconds,
                )
                self._normalize_phase(phase, output)
                with self._lock:
                    if self.state == state:
                        self._promote(state, output)
                    self._normalized[state] = output
                    self._sources[state] = {**source_status, "status": "ready"}
                    return self.snapshot()
        except Exception as exc:
            shutil.rmtree(output.parent, ignore_errors=True)
            with self._lock:
                self._sources[state] = {
                    **source_status,
                    "status": "error",
                    "error": str(exc),
                }
            if isinstance(exc, DemoStreamError):
                raise
            raise DemoStreamError(
                f"Could not prepare demo source for '{state}': {exc}"
            ) from exc

    def _stop_process(self) -> None:
        process = self._process
        log_handle = self._log_handle
        self._process = None
        self._log_handle = None
        self._stop_candidate(process, log_handle)

    def close(self) -> None:
        with self._lock:
            self._stop_process()

    def _latest_segment(self) -> dict[str, Any] | None:
        found: list[tuple[int, Path]] = []
        for path in self.hls_dir.glob("seg_*.ts"):
            match = SEGMENT_RE.match(path.name)
            if match:
                found.append((int(match.group(1)), path))
        found.sort(key=lambda item: item[0])
        if not found:
            return None
        sequence, path = found[-2] if len(found) > 1 else found[-1]
        phase = self.scenario.phase_for_state(self.state)
        assert phase is not None
        return {
            "sequence": sequence,
            "phase": phase.name,
            "expected_state": phase.label if self.state != "auto" else None,
            "internal_url": f"{self.internal_base_url}/hls/{path.name}",
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            process_ok = self._process is not None and self._process.poll() is None
            playlist_ok = (self.hls_dir / "playlist.m3u8").is_file()
            process_failed = self._process is not None and self._process.poll() is not None
            if process_ok and playlist_ok:
                stream_status = "ready"
            elif process_failed:
                stream_status = "error"
            else:
                stream_status = "starting"
            latest = self._latest_segment()
            return {
                "ok": bool(process_ok),
                "status": stream_status,
                "session_id": self.session_id,
                "demo_state": self.state,
                "available_states": list(self.scenario.available_states),
                "scenario": scenario_as_dict(self.scenario),
                "source": {
                    "type": self.source_type,
                    **self._sources.get(
                        self.state,
                        {"mode": self.state, "status": "unavailable"},
                    ),
                },
                "sources": {key: dict(value) for key, value in self._sources.items()},
                "current_phase": latest["phase"] if latest else None,
                "hls_url": self.public_hls_url,
                "segment": latest,
            }
