import json

from django.core.management.base import BaseCommand

from core.flood_camera_monitoring.infra.models import Camera, OperationalAlert
from core.flood_camera_monitoring.services.operational_alerts import canonical_region_for_camera


class Command(BaseCommand):
    help = "Lista câmeras e alertas ativos com associação territorial pendente, sem alterar dados."

    def handle(self, *args, **options):
        cameras = Camera.objects.select_related(
            "city",
            "region",
            "neighborhood__region",
            "address__city_ref",
            "address__neighborhood__region",
        ).filter(status=Camera.CameraStatus.ACTIVE)
        pending = []
        for camera in cameras:
            resolved = canonical_region_for_camera(camera)
            resolution = camera.territory_resolution or {}
            if resolved and resolution.get("resolved") is not False:
                continue
            pending.append({
                "camera_id": str(camera.id),
                "description": camera.description,
                "camera_region_id": str(camera.region_id) if camera.region_id else None,
                "resolved_region_id": str(resolved.id) if resolved else None,
                "territory_resolution": resolution,
            })

        alerts_without_region = (
            OperationalAlert.objects.filter(
                status__in=(
                    OperationalAlert.Status.OPEN_INDICATION,
                    OperationalAlert.Status.CONFIRMED,
                ),
                region__isnull=True,
            )
            .select_related(
                "camera__region",
                "camera__neighborhood__region",
                "camera__address__neighborhood__region",
            )
        )
        self.stdout.write(json.dumps({
            "pending_cameras": pending,
            "active_alerts_without_region": [
                {
                    "alert_id": str(alert.id),
                    "camera_id": str(alert.camera_id),
                    "camera_description": alert.camera.description,
                    "resolved_region_id": (
                        str(region.id)
                        if (region := canonical_region_for_camera(alert.camera))
                        else None
                    ),
                }
                for alert in alerts_without_region
            ],
        }, ensure_ascii=False, indent=2, default=str))
