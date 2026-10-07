from rest_framework.views import APIView
from rest_framework.response import Response
from core.forecast.presentation.tasks.ForecastTasks import forecast
from core.forecast.models import Forecast
from core.forecast.presentation.serializers.ForecastSerializer import ForecastSerializer

class PredictView(APIView):
    def post(self, request):
        forecast.delay()
        return Response({"mensagem": "Previsões recalculadas."}, status=202)
    
    def get(self, request):
        forecasts = Forecast.objects.all()
        serializer = ForecastSerializer(forecasts, many=True)
        return Response(serializer.data)