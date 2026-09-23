from rest_framework import serializers

import uuid

from django.contrib.gis.geos import Point
from core.addressing.models import Neighborhood, Region
from core.addressing.services import TerritoryResolutionError, TerritoryResolver

from .models import NotificationEvent, NotificationEventAudit, PushDelivery, PushSubscription, RegionSubscription, SavedPlace


class RegionSummarySerializer(serializers.ModelSerializer):
    city = serializers.SerializerMethodField()

    @staticmethod
    def get_city(region):
        return {
            "id": str(region.city_ref_id) if region.city_ref_id else "",
            "name": region.city,
        }

    class Meta:
        model = Region
        fields = ("id", "name", "city")


class RegionSubscriptionSerializer(serializers.ModelSerializer):
    region_id = serializers.PrimaryKeyRelatedField(
        source="region", queryset=Region.objects.filter(is_active=True), write_only=True
    )
    region = RegionSummarySerializer(read_only=True)
    neighborhood_id = serializers.PrimaryKeyRelatedField(
        source="neighborhood", queryset=Neighborhood.objects.filter(is_active=True),
        write_only=True, required=False,
    )
    neighborhood = serializers.SerializerMethodField()

    class Meta:
        model = RegionSubscription
        fields = ("id", "region_id", "region", "neighborhood_id", "neighborhood", "created_at")
        read_only_fields = ("id", "created_at")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["region_id"].required = False

    def validate(self, attrs):
        if bool(attrs.get("region")) == bool(attrs.get("neighborhood")):
            raise serializers.ValidationError("Informe exatamente uma região ou bairro.")
        return attrs

    @staticmethod
    def get_neighborhood(instance):
        item = instance.neighborhood
        if not item:
            return None
        return {"id": str(item.id), "name": item.name, "city": item.city,
                "region_id": str(item.region_id) if item.region_id else None}


class RegionSubscriptionDeleteSerializer(serializers.Serializer):
    region_id = serializers.PrimaryKeyRelatedField(
        source="region", queryset=Region.objects.all()
    )
    neighborhood_id = serializers.PrimaryKeyRelatedField(
        source="neighborhood", queryset=Neighborhood.objects.all(), required=False
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["region_id"].required = False

    def validate(self, attrs):
        if bool(attrs.get("region")) == bool(attrs.get("neighborhood")):
            raise serializers.ValidationError("Informe exatamente uma região ou bairro.")
        return attrs


class SavedPlaceSerializer(serializers.ModelSerializer):
    latitude = serializers.FloatField(min_value=-90, max_value=90, required=False)
    longitude = serializers.FloatField(min_value=-180, max_value=180, required=False)
    territory = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = SavedPlace
        fields = ("id", "name", "latitude", "longitude", "radius_km", "territory", "created_at", "updated_at")
        read_only_fields = ("id", "territory", "created_at", "updated_at")

    def validate(self, attrs):
        latitude = attrs.pop("latitude", None)
        longitude = attrs.pop("longitude", None)
        if self.instance is None and (latitude is None or longitude is None):
            raise serializers.ValidationError({"location": "Latitude e longitude são obrigatórias."})
        if (latitude is None) != (longitude is None):
            raise serializers.ValidationError({"location": "Informe latitude e longitude juntas."})
        if latitude is None:
            return attrs
        point = Point(longitude, latitude, srid=4326)
        try:
            resolution = TerritoryResolver().resolve_point(point)
        except TerritoryResolutionError as exc:
            raise serializers.ValidationError({"location": str(exc)}) from exc
        attrs.update(location=point, city=resolution.city, region=resolution.region,
                     neighborhood=resolution.neighborhood)
        return attrs

    @staticmethod
    def get_territory(instance):
        return {
            "city": instance.city.name if instance.city else None,
            "region": instance.region.name if instance.region else None,
            "neighborhood": instance.neighborhood.name if instance.neighborhood else None,
        }

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["latitude"] = instance.location.y
        data["longitude"] = instance.location.x
        return data


class NotificationEventSerializer(serializers.ModelSerializer):
    region_ids = serializers.PrimaryKeyRelatedField(source="regions", many=True, queryset=Region.objects.filter(is_active=True), write_only=True, required=False)
    neighborhood_ids = serializers.PrimaryKeyRelatedField(source="neighborhoods", many=True, queryset=Neighborhood.objects.filter(is_active=True), write_only=True, required=False)
    regions = RegionSummarySerializer(many=True, read_only=True)
    neighborhoods = serializers.SerializerMethodField()
    delivery_summary = serializers.SerializerMethodField()

    class Meta:
        model = NotificationEvent
        fields = ("id", "origin", "status", "severity", "title", "message", "destination_url",
                  "is_global", "region_ids", "neighborhood_ids", "regions", "neighborhoods", "audience_count",
                  "delivery_summary", "created_at", "published_at", "resolved_at")
        read_only_fields = ("id", "origin", "status", "audience_count", "created_at", "published_at", "resolved_at")

    def validate(self, attrs):
        regions = attrs.get("regions", getattr(self.instance, "regions", []).all() if self.instance else [])
        neighborhoods = attrs.get("neighborhoods", getattr(self.instance, "neighborhoods", []).all() if self.instance else [])
        is_global = attrs.get("is_global", getattr(self.instance, "is_global", False))
        if is_global and (regions or neighborhoods):
            raise serializers.ValidationError("Comunicados globais não devem selecionar regiões ou bairros.")
        if not is_global and not regions and not neighborhoods:
            raise serializers.ValidationError("Informe ao menos uma região ou bairro.")
        return attrs

    def create(self, validated_data):
        regions = validated_data.pop("regions", [])
        neighborhoods = validated_data.pop("neighborhoods", [])
        validated_data.update(origin=NotificationEvent.Origin.MANUAL,
                              actor=self.context["request"].user,
                              idempotency_key=f"manual:{uuid.uuid4()}")
        event = super().create(validated_data)
        event.regions.set(regions)
        event.neighborhoods.set(neighborhoods)
        NotificationEventAudit.objects.create(
            event=event, action="CREATED", actor=self.context["request"].user
        )
        return event

    @staticmethod
    def get_neighborhoods(instance):
        return [{"id": str(item.id), "name": item.name, "city": item.city} for item in instance.neighborhoods.all()]

    @staticmethod
    def get_delivery_summary(instance):
        counts = {key: 0 for key in ("pending", "sent", "failed", "expired")}
        for status in instance.push_deliveries.values_list("status", flat=True):
            counts[status] = counts.get(status, 0) + 1
        return counts


class PushSubscriptionSerializer(serializers.ModelSerializer):
    endpoint = serializers.URLField(max_length=2048, write_only=True)
    keys = serializers.DictField(write_only=True)

    class Meta:
        model = PushSubscription
        fields = ("id", "endpoint", "keys", "is_active", "created_at", "updated_at")
        read_only_fields = ("id", "is_active", "created_at", "updated_at")

    def validate_keys(self, value):
        p256dh = value.get("p256dh")
        auth = value.get("auth")
        if not isinstance(p256dh, str) or not p256dh.strip():
            raise serializers.ValidationError({"p256dh": "Chave obrigatória."})
        if not isinstance(auth, str) or not auth.strip():
            raise serializers.ValidationError({"auth": "Chave obrigatória."})
        if len(p256dh) > 512 or len(auth) > 512:
            raise serializers.ValidationError("Chave excede o tamanho permitido.")
        return value

    def create(self, validated_data):
        keys = validated_data.pop("keys")
        request = self.context["request"]
        subscription, _ = PushSubscription.objects.update_or_create(
            endpoint=validated_data["endpoint"],
            defaults={
                "user": request.user,
                "p256dh": keys["p256dh"],
                "auth": keys["auth"],
                "user_agent": request.headers.get("User-Agent", "")[:512],
                "is_active": True,
                "expired_at": None,
            },
        )
        return subscription


class PushSubscriptionDeleteSerializer(serializers.Serializer):
    endpoint = serializers.URLField(max_length=2048)


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)


class ResolveSerializer(ReasonSerializer):
    notify_subscribers = serializers.BooleanField(default=True)


class PushDeliverySummarySerializer(serializers.Serializer):
    pending = serializers.IntegerField()
    sent = serializers.IntegerField()
    failed = serializers.IntegerField()
    expired = serializers.IntegerField()


class OperationalAlertFilterSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=("OPEN_INDICATION", "CONFIRMED", "DISMISSED", "RESOLVED"),
        required=False,
    )
    region = serializers.UUIDField(required=False)
    camera = serializers.UUIDField(required=False)
    date_from = serializers.DateTimeField(required=False)
    date_to = serializers.DateTimeField(required=False)
    region_id = serializers.UUIDField(required=False)
    neighborhood_id = serializers.UUIDField(required=False)
    camera_id = serializers.UUIDField(required=False)
    detected_from = serializers.DateTimeField(required=False)
    detected_to = serializers.DateTimeField(required=False)
    ordering = serializers.ChoiceField(
        choices=("first_detected_at", "-first_detected_at", "last_detected_at", "-last_detected_at"),
        required=False,
    )

    def validate(self, attrs):
        attrs["region"] = attrs.get("region_id", attrs.get("region"))
        attrs["camera"] = attrs.get("camera_id", attrs.get("camera"))
        attrs["date_from"] = attrs.get("detected_from", attrs.get("date_from"))
        attrs["date_to"] = attrs.get("detected_to", attrs.get("date_to"))
        if attrs.get("date_from") and attrs.get("date_to") and attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError("date_from deve ser anterior a date_to.")
        return attrs
