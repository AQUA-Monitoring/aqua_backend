from django.urls import path, include
from rest_framework.routers import DefaultRouter
from core.weather.presentation.views import WeatherAPIView, WeatherViewSet

router = DefaultRouter(trailing_slash=False)
router.register("weather", WeatherViewSet, basename="weather")

urlpatterns = [
    path("", WeatherAPIView.as_view(), name="weather_api_view"),
    path("", include(router.urls)),
]
