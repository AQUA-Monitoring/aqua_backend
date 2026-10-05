from django.urls import path, include
from core.weather.presentation.views import WeatherAPIView

urlpatterns = [
    path("", WeatherAPIView.as_view(), name="weather_api_view"),
]
