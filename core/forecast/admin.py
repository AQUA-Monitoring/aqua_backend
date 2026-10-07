from django.contrib import admin
from core.forecast.models import Forecast

@admin.register(Forecast)
class ForecastAdmin(admin.ModelAdmin):
    list_display = ("datetime", "latitude", "longitude", "probability")
    list_filter = ("datetime",)
    search_fields = ("latitude", "longitude")