from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings


class DemoStreamUnavailable(RuntimeError):
    pass


@dataclass
class DemoStreamClient:
    base_url: str = ""
    control_token: str = ""
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.base_url:
            self.base_url = str(settings.DEMO_STREAM_INTERNAL_URL)
        if not self.control_token:
            self.control_token = str(settings.DEMO_CONTROL_TOKEN)

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Demo-Control-Token": self.control_token}

    def get_state(self) -> dict[str, Any]:
        return self._request("get", "/state")

    def set_state(self, state: str) -> dict[str, Any]:
        return self._request("post", "/state", json={"state": state})

    def prepare_source(self, mode: str, attachment_key: str) -> dict[str, Any]:
        """Request sidecar preparation; callers should run this off the HTTP thread."""
        return self._request(
            "put",
            f"/sources/{mode}",
            json={"attachment_key": attachment_key},
            # Preparation is already asynchronous from the HTTP API because this
            # call runs in Celery. Do not abandon a sidecar normalization that can
            # legitimately take several minutes and then diverge DB from HLS.
            timeout=(self.timeout_seconds, None),
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            timeout = kwargs.pop("timeout", self.timeout_seconds)
            response = requests.request(
                method,
                f"{self.base_url.rstrip('/')}{path}",
                headers=self.headers,
                timeout=timeout,
                **kwargs,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise DemoStreamUnavailable(f"Demo stream unavailable: {exc}") from exc
        if not isinstance(payload, dict):
            raise DemoStreamUnavailable("Demo stream returned an invalid response")
        return payload
