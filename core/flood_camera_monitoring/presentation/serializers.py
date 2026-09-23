from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

from rest_framework import serializers

from core.flood_camera_monitoring.services.operational_snapshot import (
    snapshot_prediction_payload,
    snapshot_stale_seconds,
)
from core.flood_camera_monitoring.infra.models import Camera, CameraOperationalSnapshot


def operational_stale_after_seconds() -> int:
    return snapshot_stale_seconds()


def normalize_hls_url(value: str) -> str:
    """Normaliza a identidade textual do stream sem remover query de acesso."""

    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


class RejectUnknownFieldsMixin:
    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            unknown = sorted(set(data.keys()) - set(self.fields.keys()))
            if unknown:
                raise serializers.ValidationError(
                    {name: "Campo não permitido." for name in unknown}
                )
        return super().to_internal_value(data)


class CameraAddressInputSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    city_id = serializers.UUIDField()
    neighborhood_id = serializers.UUIDField()
    street_id = serializers.UUIDField(required=False, allow_null=True)
    address_reference_id = serializers.UUIDField(required=False, allow_null=True)
    street = serializers.CharField(max_length=255, allow_blank=False)
    number = serializers.CharField(max_length=50, required=False, allow_blank=True)
    state = serializers.CharField(max_length=80, required=False, allow_blank=True)
    country = serializers.CharField(
        max_length=120, required=False, allow_blank=False, default="Brazil"
    )
    zipcode = serializers.CharField(max_length=32, required=False, allow_blank=True)
    latitude = serializers.FloatField(min_value=-90.0, max_value=90.0)
    longitude = serializers.FloatField(min_value=-180.0, max_value=180.0)


class CameraUpdateAddressInputSerializer(CameraAddressInputSerializer):
    neighborhood_id = serializers.UUIDField(required=False, allow_null=True)


class CameraCreateSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    description = serializers.CharField(max_length=255, allow_blank=False)
    video_hls = serializers.URLField(max_length=512, allow_blank=False)
    video_embed = serializers.URLField(
        max_length=512, required=False, allow_blank=True, allow_null=True
    )
    address = CameraAddressInputSerializer()

    def validate_video_hls(self, value: str) -> str:
        normalized = normalize_hls_url(value)
        if urlsplit(normalized).scheme not in {"http", "https"}:
            raise serializers.ValidationError("Use uma URL HTTP ou HTTPS.")
        return normalized


class CameraUpdateSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    """Campos mutáveis da câmera, exclusivos da administração."""

    description = serializers.CharField(max_length=255, required=False, allow_blank=False)
    video_hls = serializers.URLField(max_length=512, required=False, allow_blank=False)
    video_embed = serializers.URLField(max_length=512, required=False, allow_blank=True, allow_null=True)
    status = serializers.ChoiceField(choices=Camera.CameraStatus.names, required=False)
    address = CameraUpdateAddressInputSerializer(required=False)

    def validate_video_hls(self, value: str) -> str:
        normalized = normalize_hls_url(value)
        if urlsplit(normalized).scheme not in {"http", "https"}:
            raise serializers.ValidationError("Use uma URL HTTP ou HTTPS.")
        return normalized

    def validate_status(self, value: str) -> int:
        return Camera.CameraStatus[value].value


class NearbyCamerasQuerySerializer(serializers.Serializer):
    radius_m = serializers.IntegerField(
        required=False, min_value=100, max_value=20_000, default=5_000
    )
    page = serializers.IntegerField(required=False, min_value=1, default=1)
    page_size = serializers.IntegerField(
        required=False, min_value=1, max_value=20, default=6
    )


class StreamSnapshotSerializer(serializers.Serializer):
    stream_url = serializers.CharField()
    timeout_seconds = serializers.FloatField(required=False, min_value=0.5, default=5.0)


class StreamBatchSerializer(serializers.Serializer):
    stream_url = serializers.CharField()
    interval_seconds = serializers.FloatField(
        required=False, min_value=0.1, default=2.0
    )
    max_iterations = serializers.IntegerField(
        required=False, min_value=1, max_value=20, default=3
    )


class PredictAllCamerasResponseSerializer(serializers.Serializer):
    camera = serializers.DictField()
    is_flooded = serializers.BooleanField(allow_null=True)
    confidence = serializers.FloatField(allow_null=True)
    medium = serializers.BooleanField(allow_null=True)
    probabilities = serializers.DictField()
    meta = serializers.DictField(required=False)


class DemoStateSerializer(serializers.Serializer):
    """Validate the only mutable field exposed by the demo control API."""

    state = serializers.ChoiceField(choices=("auto", "normal", "flooded"))


class DemoPredictionQuerySerializer(serializers.Serializer):
    """Select the exact HLS segment currently rendered by the demo player."""

    sequence = serializers.IntegerField(required=False, min_value=0)


class DemoPredictionBatchSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    """Validate a temporal comparison anchored to the rendered HLS segment."""

    session_id = serializers.CharField(max_length=128, allow_blank=False)
    anchor_sequence = serializers.IntegerField(min_value=0)
    model_version = serializers.CharField(
        required=False, max_length=128, allow_blank=False
    )


def _date_time(value):
    if value is None:
        return None
    return serializers.DateTimeField().to_representation(value)


def _snapshot_for(camera):
    try:
        return camera.operational_snapshot
    except CameraOperationalSnapshot.DoesNotExist:
        return None


def build_operational_payload(camera) -> dict:
    snapshot = _snapshot_for(camera)
    projection = snapshot_prediction_payload(camera, snapshot)
    metadata = projection["meta"]
    raw_probabilities = projection["probabilities"]
    probabilities = (
        None
        if all(value is None for value in raw_probabilities.values())
        else raw_probabilities
    )

    return {
        "stream": {
            "status": metadata["stream_status"],
            "checked_at": metadata["stream_checked_at"],
        },
        "analysis": {
            "status": projection["status"],
            "classification": projection["classification"],
            "probabilities": probabilities,
            "confidence": projection["confidence"],
            "frames": metadata["frames"],
            "started_at": _date_time(
                snapshot.analysis_started_at if snapshot else None
            ),
            "analyzed_at": metadata["analyzed_at"],
            "model": {
                "status": metadata["model_status"],
                "version": metadata["model_version"],
            },
            "error_code": metadata["error_code"],
        },
        "updated_at": _date_time(snapshot.updated_at if snapshot else None),
    }


def build_address_payload(camera):
    address = camera.address
    if address is None:
        return None

    neighborhood = address.neighborhood
    region = neighborhood.region if neighborhood else None
    city_ref = address.city_ref
    return {
        "id": str(address.id),
        "street_id": str(address.street_ref_id) if address.street_ref_id else None,
        "address_reference_id": (
            str(address.address_reference_id)
            if address.address_reference_id
            else None
        ),
        "street": address.street,
        "number": address.number,
        "city": address.city,
        "city_ref": (
            {"id": str(city_ref.id), "name": city_ref.name} if city_ref else None
        ),
        "state": address.state,
        "country": address.country,
        "zipcode": address.zipcode,
        "latitude": address.latitude,
        "longitude": address.longitude,
        "neighborhood": (
            {"id": str(neighborhood.id), "name": neighborhood.name}
            if neighborhood
            else None
        ),
        "region": (
            {"id": str(region.id), "name": region.name} if region else None
        ),
    }


class CameraReadSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    description = serializers.CharField(read_only=True)
    status = serializers.SerializerMethodField()
    administrative_status = serializers.SerializerMethodField()
    video_hls = serializers.CharField(read_only=True, allow_null=True)
    preview_url = serializers.SerializerMethodField()
    video_embed = serializers.CharField(read_only=True, allow_null=True)
    address = serializers.SerializerMethodField()
    neighborhood = serializers.SerializerMethodField()
    region = serializers.SerializerMethodField()
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()
    operational = serializers.SerializerMethodField()
    monitoring = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
    created_by = serializers.SerializerMethodField()
    territorial_context = serializers.SerializerMethodField()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self.context.get("include_created_by", False):
            data.pop("created_by", None)
        if (
            instance.status == instance.CameraStatus.INACTIVE
            and not self.context.get("include_inactive_sources", False)
        ):
            data["video_hls"] = None
            data["video_embed"] = None
            data["preview_url"] = None
        return data

    @staticmethod
    def get_status(camera):
        return camera.get_status_display()

    @staticmethod
    def get_administrative_status(camera):
        if camera.status == camera.CameraStatus.INACTIVE:
            return "INACTIVE"
        return "ACTIVE"

    @staticmethod
    def get_address(camera):
        return build_address_payload(camera)

    @staticmethod
    def _legacy_location(camera):
        if camera.address is not None:
            neighborhood = camera.address.neighborhood
            return (
                neighborhood,
                camera.address.latitude,
                camera.address.longitude,
            )
        return camera.neighborhood, camera.latitude, camera.longitude

    def get_neighborhood(self, camera):
        neighborhood, _, _ = self._legacy_location(camera)
        if neighborhood is None:
            return None
        return {"id": str(neighborhood.id), "name": neighborhood.name}

    def get_region(self, camera):
        if camera.region_id:
            return {"id": str(camera.region_id), "name": camera.region.name}
        neighborhood, _, _ = self._legacy_location(camera)
        region = neighborhood.region if neighborhood else None
        if region is None:
            return None
        return {"id": str(region.id), "name": region.name}

    def get_latitude(self, camera):
        _, latitude, _ = self._legacy_location(camera)
        return latitude

    def get_longitude(self, camera):
        _, _, longitude = self._legacy_location(camera)
        return longitude

    @staticmethod
    def get_operational(camera):
        return build_operational_payload(camera)

    @staticmethod
    def get_monitoring(camera):
        state = getattr(camera, 'monitoring', None)
        if state is None:
            return None
        return {'level': state.level, 'reason': state.reason,
            'next_analysis_at': state.next_analysis_at,
            'strong_streak': state.strong_streak}

    @staticmethod
    def get_created_by(camera):
        if camera.created_by_id is None:
            return None
        return {"id": str(camera.created_by_id)}

    @staticmethod
    def get_territorial_context(camera):
        return {
            "city_id": str(camera.city_id) if camera.city_id else None,
            "region_id": str(camera.region_id) if camera.region_id else None,
            "neighborhood_id": str(camera.neighborhood_id) if camera.neighborhood_id else None,
            "street_id": str(camera.street_id) if camera.street_id else None,
            "road_segment_id": str(camera.road_segment_id) if camera.road_segment_id else None,
            "address_reference_id": str(camera.address_reference_id) if camera.address_reference_id else None,
            "resolution": camera.territory_resolution or {},
        }

    @staticmethod
    def get_preview_url(camera):
        """Expose only the explicitly authorized preview source in summaries.

        The list contract is additive and keeps the detail-only ``video_hls``
        field intact for clients that need the full camera inspection route.
        Inactive cameras never receive a playable preview.
        """
        if camera.status == camera.CameraStatus.INACTIVE:
            return None
        return camera.video_hls or None


class CameraListSerializer(CameraReadSerializer):
    """Resumo público sem endereços de reprodução nem autoria."""

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data.pop("video_hls", None)
        data.pop("video_embed", None)
        data.pop("created_by", None)
        return data


def build_legacy_prediction_payload(camera) -> dict:
    projection = snapshot_prediction_payload(camera, _snapshot_for(camera))
    metadata = projection["meta"]

    return {
        "camera": projection["camera"],
        "is_flooded": projection["is_flooded"],
        "confidence": projection["confidence"],
        "medium": projection["medium"],
        "probabilities": projection["probabilities"],
        "meta": {
            "frames": metadata["frames"],
            "status": projection["status"],
            "classification": projection["classification"],
            "analyzed_at": metadata["analyzed_at"],
            "model": {
                "status": metadata["model_status"],
                "version": metadata["model_version"],
            },
        },
    }
