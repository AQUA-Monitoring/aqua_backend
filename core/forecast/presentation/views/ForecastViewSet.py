from rest_framework.viewsets import ModelViewSet
from core.forecast.models import Forecast
from core.forecast.presentation.serializers.ForecastSerializer import ForecastSerializer

class ForecastViewSet(ModelViewSet):
    queryset = Forecast.objects.all()
    serializer_class = ForecastSerializer
    # Deixa o Forecast livre para um CRUD manual
