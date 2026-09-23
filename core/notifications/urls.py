from django.urls import path

from rest_framework.routers import DefaultRouter
from .views import NotificationEventViewSet, OperationalAlertViewSet, PushSubscriptionViewSet, RegionSubscriptionViewSet, SavedPlaceViewSet


region_list = RegionSubscriptionViewSet.as_view(
    {"get": "list", "post": "create", "delete": "destroy_collection"}
)
push_create = PushSubscriptionViewSet.as_view(
    {"post": "create", "delete": "destroy_collection"}
)
push_config = PushSubscriptionViewSet.as_view({"get": "config"})
alert_list = OperationalAlertViewSet.as_view({"get": "list"})
alert_confirm = OperationalAlertViewSet.as_view({"post": "confirm"})
alert_dismiss = OperationalAlertViewSet.as_view({"post": "dismiss"})
alert_resolve = OperationalAlertViewSet.as_view({"post": "resolve"})

urlpatterns = [
    path("region-subscriptions/", region_list, name="region-subscription-list"),
    path("push-subscriptions/", push_create, name="push-subscription-create"),
    path("push-subscriptions/config/", push_config, name="push-subscription-config"),
    path("operational-alerts/", alert_list, name="operational-alert-list"),
    path("operational-alerts/<uuid:pk>/confirm/", alert_confirm, name="operational-alert-confirm"),
    path("operational-alerts/<uuid:pk>/dismiss/", alert_dismiss, name="operational-alert-dismiss"),
    path("operational-alerts/<uuid:pk>/resolve/", alert_resolve, name="operational-alert-resolve"),
]

router = DefaultRouter()
router.register("saved-places", SavedPlaceViewSet, basename="saved-place")
router.register("notification-events", NotificationEventViewSet, basename="notification-event")
urlpatterns += router.urls
