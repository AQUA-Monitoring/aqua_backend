from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from core.flood_camera_monitoring.demo.controller import (
    DemoStreamController,
    DemoStreamError,
)


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class _BaseHandler(BaseHTTPRequestHandler):
    server_version = "AquaDemo/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        logging.getLogger(__name__).info("%s - %s", self.address_string(), format % args)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def make_media_handler(
    controller: DemoStreamController,
) -> type[BaseHTTPRequestHandler]:
    class MediaHandler(_BaseHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/health":
                snapshot = controller.snapshot()
                self._send_json(
                    {"ok": bool(snapshot.get("ok")), "status": snapshot.get("status")},
                    status=200 if snapshot.get("ok") else 503,
                )
                return
            if not path.startswith("/hls/"):
                self._send_json({"detail": "Not found"}, status=404)
                return

            relative = unquote(path.removeprefix("/hls/")).lstrip("/")
            root = controller.hls_dir.resolve()
            candidate = (root / relative).resolve()
            if candidate == root or root not in candidate.parents or not candidate.is_file():
                self._send_json({"detail": "Not found"}, status=404)
                return

            content_type = {
                ".m3u8": "application/vnd.apple.mpegurl",
                ".ts": "video/mp2t",
            }.get(candidate.suffix, mimetypes.guess_type(candidate.name)[0])
            data = candidate.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return MediaHandler


def make_control_handler(
    controller: DemoStreamController, control_token: str
) -> type[BaseHTTPRequestHandler]:
    class ControlHandler(_BaseHandler):
        def _authorized(self) -> bool:
            supplied = self.headers.get("X-Demo-Control-Token", "")
            return bool(control_token) and hmac.compare_digest(supplied, control_token)

        def _require_authorized(self) -> bool:
            if self._authorized():
                return True
            self._send_json({"detail": "Unauthorized"}, status=401)
            return False

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/health":
                snapshot = controller.snapshot()
                self._send_json(
                    {"ok": bool(snapshot.get("ok")), "status": snapshot.get("status")},
                    status=200 if snapshot.get("ok") else 503,
                )
                return
            if path != "/state":
                self._send_json({"detail": "Not found"}, status=404)
                return
            if not self._require_authorized():
                return
            self._send_json(controller.snapshot())

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/state":
                self._send_json({"detail": "Not found"}, status=404)
                return
            if not self._require_authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                state = payload.get("state") if isinstance(payload, dict) else None
                if not isinstance(state, str):
                    raise DemoStreamError("'state' must be a string")
                self._send_json(controller.set_state(state))
            except (json.JSONDecodeError, DemoStreamError, ValueError) as exc:
                self._send_json({"detail": str(exc)}, status=400)

        def do_PUT(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            prefix = "/sources/"
            if not path.startswith(prefix):
                self._send_json({"detail": "Not found"}, status=404)
                return
            if not self._require_authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                attachment_key = (
                    payload.get("attachment_key") if isinstance(payload, dict) else None
                )
                if not isinstance(attachment_key, str) or not attachment_key.strip():
                    raise DemoStreamError("'attachment_key' must be a non-empty string")
                self._send_json(
                    controller.prepare_source(
                        unquote(path.removeprefix(prefix)), attachment_key.strip()
                    )
                )
            except (json.JSONDecodeError, DemoStreamError, ValueError) as exc:
                self._send_json({"detail": str(exc)}, status=400)

    return ControlHandler


class DemoServers:
    def __init__(
        self,
        controller: DemoStreamController,
        *,
        media_host: str,
        media_port: int,
        control_host: str,
        control_port: int,
        control_token: str,
    ) -> None:
        self.media_server = ThreadingHTTPServer(
            (media_host, media_port), make_media_handler(controller)
        )
        self.control_server = ThreadingHTTPServer(
            (control_host, control_port),
            make_control_handler(controller, control_token),
        )
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for server, name in (
            (self.media_server, "demo-media-server"),
            (self.control_server, "demo-control-server"),
        ):
            thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        for server in (self.media_server, self.control_server):
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2)
