from django.urls import path, include
from rest_framework.routers import DefaultRouter
from core.addressing.presentation.viewsets import AddressingViewSet, AddressingV2ViewSet

router = DefaultRouter()
router.register(r"", AddressingViewSet, basename="addressing")

urlpatterns = [
    path("v2/status/", AddressingV2ViewSet.as_view({"get": "status"}), name="addressing-v2-status"),
    path("v2/cities/", AddressingV2ViewSet.as_view({"get": "cities"}), name="addressing-v2-cities"),
    path("v2/territories/", AddressingV2ViewSet.as_view({"get": "territories"}), name="addressing-v2-territories"),
    path("v2/streets/", AddressingV2ViewSet.as_view({"get": "streets"}), name="addressing-v2-streets"),
    path("v2/autocomplete/", AddressingV2ViewSet.as_view({"get": "autocomplete"}), name="addressing-v2-autocomplete"),
    path("v2/resolve/", AddressingV2ViewSet.as_view({"get": "resolve"}), name="addressing-v2-resolve"),
    path("v2/resolve-area/", AddressingV2ViewSet.as_view({"post": "resolve_area"}), name="addressing-v2-resolve-area"),
    path("", include(router.urls)),
]
