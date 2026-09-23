import logging

from celery import shared_task

from core.uploader.models import DemoVideoSource

logger = logging.getLogger(__name__)


@shared_task(name="core.uploader.tasks.prepare_demo_source_task")
def prepare_demo_source_task(mode: str, attachment_key: str) -> None:
    # Kept inside the task so the lightweight HTTP import path does not initialize
    # the demo integration or any future media-processing dependencies.
    from core.flood_camera_monitoring.infra.demo_stream_client import (
        DemoStreamClient,
        DemoStreamUnavailable,
    )

    try:
        payload = DemoStreamClient().prepare_source(mode, attachment_key)
        sources = payload.get("sources", {})
        source = sources.get(mode, {}) if isinstance(sources, dict) else {}
        if not source and isinstance(payload.get("source"), dict):
            source = payload["source"]
        sidecar_status = source.get("status") if isinstance(source, dict) else None
        status = {
            "ready": DemoVideoSource.Status.READY,
            "error": DemoVideoSource.Status.ERROR,
        }.get(sidecar_status, DemoVideoSource.Status.PROCESSING)
        error = (
            str(source.get("error") or "")
            if status == DemoVideoSource.Status.ERROR
            else ""
        )
        active_mode = (
            payload.get("demo_state") or payload.get("mode") or payload.get("state")
        )
        if active_mode in DemoVideoSource.Mode.values:
            DemoVideoSource.objects.exclude(mode=active_mode).update(active=False)
            DemoVideoSource.objects.filter(mode=active_mode).update(active=True)
        DemoVideoSource.objects.filter(
            mode=mode, video__attachment_key=attachment_key
        ).update(status=status, error=error)
    except DemoStreamUnavailable as exc:
        logger.warning("Demo source preparation failed for %s: %s", mode, exc)
        DemoVideoSource.objects.filter(
            mode=mode, video__attachment_key=attachment_key
        ).update(status=DemoVideoSource.Status.ERROR, error=str(exc))
    except Exception as exc:  # Keep a slot from remaining stuck in processing.
        logger.exception("Unexpected demo source preparation failure for %s", mode)
        DemoVideoSource.objects.filter(
            mode=mode, video__attachment_key=attachment_key
        ).update(status=DemoVideoSource.Status.ERROR, error=str(exc))
