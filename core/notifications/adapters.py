import json
from dataclasses import dataclass

from django.conf import settings


class PushDeliveryError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class WebPushAdapter:
    def send(self, *, subscription_info: dict, payload: dict) -> None:
        if not settings.WEB_PUSH_ENABLED:
            raise PushDeliveryError("Web Push não configurado")
        try:
            from pywebpush import WebPushException, webpush
        except ImportError as exc:  # pragma: no cover - depends on deployment package
            raise PushDeliveryError("Adaptador Web Push indisponível") from exc

        try:
            webpush(
                subscription_info=subscription_info,
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=settings.WEB_PUSH_VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.WEB_PUSH_VAPID_SUBJECT},
                ttl=settings.WEB_PUSH_TTL_SECONDS,
            )
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            raise PushDeliveryError(
                "Serviço Web Push rejeitou a entrega",
                status_code=getattr(response, "status_code", None),
            ) from exc
