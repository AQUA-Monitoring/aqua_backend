"""Saúde das dependências do serviço completo de câmeras."""

import redis
from django.conf import settings
from django.db import connections
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.flood_camera_monitoring.infra.utils import looks_like_lfs_pointer, resolve_checkpoint_path

class HealthcheckView(APIView):
    """Health endpoint: verifica modelo, DB e Redis.

    - Modelo baixado: checa existência e tamanho do checkpoint e evita LFS pointer
    - DB OK: abre um cursor e executa um SELECT 1
    - Redis OK: ping no broker configurado (CELERY_BROKER_URL)
    """

    def get(self, request, *args, **kwargs):
        # 1) Modelo
        checkpoint_path = resolve_checkpoint_path()

        model_exists = checkpoint_path.exists() and checkpoint_path.is_file()
        model_size = 0
        if model_exists:
            try:
                model_size = checkpoint_path.stat().st_size
            except Exception:
                model_size = 0
        model_ok = bool(
            model_exists
            and (model_size >= 1024 * 1024)
            and not looks_like_lfs_pointer(checkpoint_path)
        )

        # DB
        db_ok = False
        try:
            with connections["default"].cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            db_ok = True
        except Exception:
            pass

        # Redis broker
        redis_ok = False
        redis_url = getattr(settings, "CELERY_BROKER_URL", "redis://redis:6379/0")
        try:
            r = redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1)
            if r.ping():
                redis_ok = True
        except Exception:
            pass

        all_ok = model_ok and db_ok and redis_ok
        payload = {
            "status": "ok" if all_ok else "degraded",
            "model": {
                "ok": model_ok,
                "exists": model_exists,
                "size": model_size,
            },
            "database": {"ok": db_ok},
            "redis": {"ok": redis_ok},
        }
        return Response(
            payload,
            status=(
                status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
        )
