from django.contrib import admin
from core.weather.models import Weather

@admin.register(Weather)
class WeatherAdmin(admin.ModelAdmin):
    list_display = ("date", "latitude", "longitude", "neighborhood", "rain", "temperature", "humidity", "elevation")
    list_filter = ("date",)
    search_fields = ("latitude", "longitude", "neighborhood", "date")