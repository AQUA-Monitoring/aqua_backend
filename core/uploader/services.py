from django.db import transaction

from core.uploader.models import DemoVideoSource, Video


class DemoSourceBusy(RuntimeError):
    pass


def replace_demo_source(
    *, mode: str, file, description: str = "", updated_by=None
) -> DemoVideoSource:
    """Point a demo slot at a new immutable upload and retain prior videos."""
    with transaction.atomic():
        slot, _ = DemoVideoSource.objects.select_for_update().get_or_create(mode=mode)
        if slot.video_id and slot.status == DemoVideoSource.Status.PROCESSING:
            raise DemoSourceBusy(mode)
        video = Video.objects.create(file=file, description=description)
        slot.video = video
        slot.status = DemoVideoSource.Status.PROCESSING
        slot.error = ""
        slot.updated_by = updated_by
        slot.save(
            update_fields=["video", "status", "error", "updated_by", "updated_at"]
        )
    return slot
