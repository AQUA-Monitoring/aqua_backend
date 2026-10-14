"""Collect host pressure in beat, independently of busy inference workers."""
import logging
import time

from celery.beat import PersistentScheduler
from django.conf import settings
from django.db import connection, transaction
from django_celery_beat.schedulers import DatabaseScheduler


class CapacitySamplingMixin:
    _capacity_next_sample = 0.0

    def tick(self, *args, **kwargs):
        from core.flood_camera_monitoring.services.monitoring_capacity import enabled, sample_capacity
        monitoring = enabled() and (getattr(settings, 'FLOOD_ADAPTIVE_ENABLED', False) or getattr(settings, 'FLOOD_INTELLIGENCE_ENABLED', False))
        now = time.monotonic()
        if monitoring and now >= self._capacity_next_sample:
            self._capacity_next_sample = now + 10
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_try_advisory_xact_lock(%s)', [73190421])
                        if cursor.fetchone()[0]:
                            sample_capacity()
            except Exception:
                logging.getLogger(__name__).exception('Monitoring capacity sampling failed')
        delay = super().tick(*args, **kwargs)
        return min(delay, 10) if monitoring else delay


class MonitoringDatabaseScheduler(CapacitySamplingMixin, DatabaseScheduler):
    pass


class MonitoringPersistentScheduler(CapacitySamplingMixin, PersistentScheduler):
    pass
