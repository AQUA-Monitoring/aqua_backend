import json
from dataclasses import dataclass

from django.conf import settings
from requests.exceptions import RequestException


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
            from py_vapid import VapidException
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
                timeout=15,
            )
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            raise PushDeliveryError(
                f"Serviço Web Push rejeitou a entrega (HTTP {getattr(response, 'status_code', None)})",
                status_code=getattr(response, "status_code", None),
            ) from exc
        except RequestException as exc:
            raise PushDeliveryError("Falha de conexão com o serviço Web Push") from exc
        except VapidException as exc:
            raise PushDeliveryError(
                "Configuração VAPID inválida; verifique as chaves e WEB_PUSH_VAPID_SUBJECT"
            ) from exc
        except ValueError as exc:
            raise PushDeliveryError("Configuração Web Push inválida") from exc
