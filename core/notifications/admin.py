from django.contrib import admin

from .models import NotificationEvent, NotificationEventAudit, PushDelivery, PushSubscription, RegionSubscription, SavedPlace


@admin.register(RegionSubscription)
class RegionSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "region", "neighborhood", "created_at")
    search_fields = ("user__email", "region__name", "neighborhood__name")


@admin.register(SavedPlace)
class SavedPlaceAdmin(admin.ModelAdmin):
    list_display = ("user", "name", "radius_km", "city", "region", "created_at")
    exclude = ("location",)
    search_fields = ("user__email", "name")


@admin.register(NotificationEvent)
class NotificationEventAdmin(admin.ModelAdmin):
    list_display = ("title", "origin", "severity", "status", "audience_count", "published_at")
    list_filter = ("origin", "severity", "status")
    readonly_fields = ("idempotency_key", "territory_snapshot", "audience_count", "published_at", "resolved_at")


@admin.register(NotificationEventAudit)
class NotificationEventAuditAdmin(admin.ModelAdmin):
    list_display = ("event", "action", "actor", "created_at")
    readonly_fields = ("event", "action", "actor", "metadata", "created_at")


@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "is_active", "created_at", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("user__email",)
    exclude = ("endpoint", "p256dh", "auth")


@admin.register(PushDelivery)
class PushDeliveryAdmin(admin.ModelAdmin):
    list_display = ("event", "operational_alert", "kind", "status", "attempts", "created_at")
    list_filter = ("kind", "status")
    readonly_fields = ("event", "operational_alert", "subscription", "kind", "attempts", "last_error")
