"""Public errors never include exception text, SQL, provider payloads or credentials."""
import json
import logging
import os
import time
import uuid
from redis.exceptions import RedisError
from pathlib import Path

from django.conf import settings
from django.db import InterfaceError, OperationalError
from django.http import JsonResponse
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler, set_rollback

logger = logging.getLogger(__name__)
MESSAGES = {
    400: ('VALIDATION_ERROR', 'Confira os campos informados e tente novamente.'),
    401: ('AUTHENTICATION_REQUIRED', 'Entre novamente para continuar.'),
    403: ('PERMISSION_DENIED', 'Sua conta não tem permissão para esta ação.'),
    404: ('RESOURCE_NOT_FOUND', 'O item solicitado não foi encontrado.'),
    405: ('METHOD_NOT_ALLOWED', 'Esta operação não está disponível.'),
    409: ('RESOURCE_CONFLICT', 'Os dados foram alterados ou já existem. Atualize a página.'),
    413: ('PAYLOAD_TOO_LARGE', 'O arquivo enviado ultrapassa o tamanho permitido.'),
    422: ('VALIDATION_ERROR', 'Confira os campos informados e tente novamente.'),
    429: ('RATE_LIMITED', 'Há muitas solicitações. Aguarde um pouco e tente novamente.'),
}


def exception_handler(exc, context):
    if isinstance(exc, (OperationalError, InterfaceError, RedisError)):
        set_rollback()
        logger.error('Dependency unavailable request_id=%s', getattr(context.get('request'), 'request_id', ''))
        return Response(status=503)
    response = drf_exception_handler(exc, context)
    if response is None:
        set_rollback()
        logger.error('Unhandled API failure type=%s request_id=%s', type(exc).__name__,
                     getattr(context.get('request'), 'request_id', ''))
        return Response(status=500)
    return response


def record_failure(component, request_id):
    directory = getattr(settings, 'OPS_EVENTS_DIR', '')
    if not directory:
        return
    try:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        target = root / f'{component}-{os.getpid()}.json'
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps({'component': component, 'request_id': request_id, 'time': time.time()}))
        os.replace(temporary, target)
    except OSError:
        # Never turn an observability failure into another API failure.
        logger.error('Cannot persist operational failure request_id=%s', request_id)


class PublicApiErrorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = str(uuid.uuid4())
        response = self.get_response(request)
        if not request.path.startswith('/api/'):
            return response
        response['X-Request-ID'] = request.request_id
        if response.status_code < 400:
            return response
        status = response.status_code
        if status >= 500:
            code, message = 'SERVICE_UNAVAILABLE', 'O serviço está temporariamente indisponível. Tente novamente em instantes.'
            component = 'cameras' if request.path.startswith('/api/flood_monitoring/') else 'api'
            record_failure(component, request.request_id)
        else:
            code, message = MESSAGES.get(status, ('REQUEST_FAILED', 'Não foi possível concluir esta operação.'))
        payload = {'code': code, 'message': message, 'detail': message,
                   'request_id': request.request_id, 'retryable': status >= 500 or status == 429}
        # Keep structured field validation usable by forms, but never expose 5xx bodies.
        if status in (400, 422):
            try:
                original = json.loads(response.content)
                if isinstance(original, dict):
                    payload['errors'] = original
            except (ValueError, AttributeError):
                pass
        normalized = JsonResponse(payload, status=status)
        for header, value in response.items():
            if header.lower() not in {'content-type', 'content-length', 'content-encoding', 'etag'}:
                normalized[header] = value
        normalized.cookies = response.cookies
        normalized['Cache-Control'] = 'no-store'
        return normalized
