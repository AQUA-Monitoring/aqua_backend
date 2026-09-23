from celery import shared_task
import logging

from core.common.cache import cache_set_json, now_ts
from django.conf import settings


@shared_task(name='core.flood_camera_monitoring.tasks.dispatch_due_cameras')
def dispatch_due_cameras():
    if not getattr(settings, 'FLOOD_ADAPTIVE_ENABLED', False):
        return 0
    from .services.adaptive_monitoring import reserve_due
    reserved = reserve_due()
    for camera_id, token, level in reserved:
        analyze_camera_task.apply_async(args=[camera_id, token],
            queue='flood_critical' if level in ('WATCH', 'CRITICAL') else 'flood_regular', expires=120)
    return len(reserved)


@shared_task(name='core.flood_camera_monitoring.tasks.analyze_camera', soft_time_limit=110, time_limit=120)
def analyze_camera_task(camera_id, token):
    from .services.adaptive_monitoring import analyze_reserved
    run_id = analyze_reserved(camera_id, token)
    if run_id:
        shadow_camera_task.apply_async(args=[run_id], queue='flood_training')
    return run_id


@shared_task(name='core.flood_camera_monitoring.tasks.shadow_camera', soft_time_limit=110, time_limit=120)
def shadow_camera_task(run_id):
    from .services.shadow import evaluate_shadow
    evaluate_shadow(run_id)


@shared_task(name='core.flood_camera_monitoring.tasks.train_candidate', soft_time_limit=3500, time_limit=3600)
def train_flood_candidate():
    from .services.retraining import train_candidate
    return train_candidate()


@shared_task(name='core.flood_camera_monitoring.tasks.expire_evidence')
def expire_camera_evidence():
    from .services.adaptive_monitoring import expire_evidence
    return expire_evidence()


@shared_task(name="core.flood_camera_monitoring.tasks.refresh_all_and_cache_task")
def refresh_all_and_cache_task() -> int:
    """Atualiza snapshots, persiste indicações e publica a projeção no cache."""
    if getattr(settings, 'FLOOD_ADAPTIVE_ENABLED', False):
        return dispatch_due_cameras()
    from core.flood_camera_monitoring.services.analyze import (
        AnalyzeAllCamerasService,
    )

    logger = logging.getLogger(__name__)
    try:
        service = AnalyzeAllCamerasService()
        data, saved = service.run_and_collect()
        payload = {"data": data, "ts": int(__import__("time").time())}
        ttl = getattr(settings, "PREDICT_CACHE_TTL_SECONDS", 300)
        cache_set_json("flood:predict_all", payload, ex=int(ttl))
        logger.info(
            "Unified refresh done: cached=%d items, persisted=%d alerts",
            len(data),
            saved,
        )
        return len(data)
    except Exception:
        logger.exception("Unified refresh failed")
        return 0


@shared_task(name="core.flood_camera_monitoring.infra.tasks.analyze_all_cameras_task")
def analyze_all_cameras_task() -> int:
    """Nome Celery legado; implementação canônica neste módulo."""
    if getattr(settings, 'FLOOD_ADAPTIVE_ENABLED', False):
        return dispatch_due_cameras()
    from core.flood_camera_monitoring.services.analyze import AnalyzeAllCamerasService

    saved = AnalyzeAllCamerasService().run()
    logging.getLogger(__name__).info(
        "AnalyzeAllCamerasTask finished: saved=%s", saved
    )
    return saved


@shared_task(
    name="core.flood_camera_monitoring.infra.tasks.refresh_predict_all_cache_task"
)
def refresh_predict_all_cache_task() -> int:
    """Nome Celery legado para atualização do cache a partir dos snapshots."""
    from core.flood_camera_monitoring.services.predict import PredictAllCamerasService

    data = PredictAllCamerasService().run()
    try:
        ttl = int(getattr(settings, "PREDICT_CACHE_TTL_SECONDS", 300))
        cache_set_json("flood:predict_all", {"data": data, "ts": now_ts()}, ex=ttl)
    except Exception:
        logging.getLogger(__name__).warning(
            "Failed to set predict_all cache", exc_info=True
        )
    return len(data)
