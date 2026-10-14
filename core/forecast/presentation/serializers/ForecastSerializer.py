from rest_framework import serializers
from core.forecast.models import Forecast

class ForecastSerializer(serializers.ModelSerializer):
    date = serializers.DateTimeField(source="datetime", format="%Y-%m-%d", read_only=True)

    class Meta:
        model = Forecast
        fields = ['latitude', 'longitude', 'datetime', 'date', 'flood', 'probability']