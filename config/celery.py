import os
import logging
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Explicitly wire broker and backend (env overrides settings if provided)
app.conf.broker_url = os.environ.get("CELERY_BROKER_URL")
app.conf.result_backend = os.environ.get("CELERY_RESULT_BACKEND")
app.conf.timezone = os.environ.get("TZ", "America/Sao_Paulo")

app.conf.beat_scheduler = "config.monitoring_scheduler:MonitoringDatabaseScheduler"

# Fallback: se migrações do django_celery_beat não foram aplicadas ainda, evita crash do beat
try:
    from django.db import connection  # type: ignore

    with connection.cursor() as cursor:  # type: ignore
        cursor.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
            ["django_celery_beat_periodictask"],
        )
        exists = cursor.fetchone()
    if not exists:
        logging.getLogger(__name__).warning(
            "Tabelas django_celery_beat ausentes. Usando scheduler em memória padrão até aplicar as migrações."
        )
        app.conf.beat_scheduler = "config.monitoring_scheduler:MonitoringPersistentScheduler"
except Exception:  # pragma: no cover - proteção defensiva
    pass


# Ensure modules are imported in case autodiscover misses nested packages
try:
    __import__("core.users.app.tasks")
except Exception:  # pragma: no cover - best effort
    pass

# Ensure flood monitoring tasks are registered
try:
    __import__("core.flood_camera_monitoring.infra.tasks")
except Exception:  # pragma: no cover - best effort
    pass
