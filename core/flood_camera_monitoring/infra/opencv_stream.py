from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, List
from pathlib import Path
from django.conf import settings
import cv2  # type: ignore

from core.flood_camera_monitoring.services.stream_prediction import VideoStream


@dataclass
class OpenCVVideoStream(VideoStream):
    url: str
    backend: int = cv2.CAP_FFMPEG

    def __post_init__(self) -> None:
        self._loop_mode = False
        self._loop_paths: List[str] = []
        # Start before the first item; _open_next_loop_source() will advance to 0
        self._loop_idx: int = -1
        self._cap = None

        url = str(self.url or "")
        if url.startswith("loop:"):
            self._loop_mode = True
            # Parse list after 'loop:' and resolve 'media:' prefix to MEDIA_ROOT
            raw = url[len("loop:") :]
            items = [p.strip() for p in raw.split(",") if p.strip()]
            for it in items:
                if it.startswith("media:"):
                    rel = it[len("media:") :].lstrip("/")
                    p = str(Path(settings.MEDIA_ROOT) / rel)
                else:
                    p = it
                self._loop_paths.append(p)
            # Open first available file lazily on first grab
            self._cap = None
        else:
            self._cap = cv2.VideoCapture(url, self.backend, [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000,
            ])
            # Best-effort low latency settings
            try:
                self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                self._cap.set(cv2.CAP_PROP_FPS, 10)
            except Exception:
                pass

    def is_open(self) -> bool:
        if self._loop_mode:
            # Lazy open if needed
            if self._cap is None:
                self._open_next_loop_source()
            return bool(self._cap is not None and self._cap.isOpened())
        return bool(self._cap is not None and self._cap.isOpened())

    def grab(self):
        if not self.is_open():
            return None

        # Loop mode: on EOF advance to next file and try again
        if self._loop_mode:
            tries = 0
            while tries < max(1, len(self._loop_paths)):
                ret, frame = self._cap.read()
                if ret and frame is not None:
                    break
                # EOF or failure: advance to next source
                self._open_next_loop_source()
                tries += 1
            else:
                return None
        else:
            ret, frame = self._cap.read()
            if not ret or frame is None:
                return None
        # Encode to JPEG bytes to comply with ImageInput (bytes OK)
        ok, buf = cv2.imencode(".jpg", frame)
        if not ok:
            return None
        return buf.tobytes()

    def close(self) -> None:
        try:
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass

    # Internal helpers
    def _open_next_loop_source(self) -> None:
        # Close current
        try:
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass
        if not self._loop_paths:
            self._cap = None
            return
        # Advance index and wrap, then open
        self._loop_idx = (self._loop_idx + 1) % len(self._loop_paths)
        path = self._loop_paths[self._loop_idx]
        cap = cv2.VideoCapture(path, self.backend)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            cap.set(cv2.CAP_PROP_FPS, 10)
        except Exception:
            pass
        self._cap = cap
