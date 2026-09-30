from rest_framework import serializers
from core.weather.models import Weather

class WeatherModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Weather
        fields = '__all__'

class WeatherFillSerializer(serializers.Serializer):
    start = serializers.DateField()
    end = serializers.DateField()