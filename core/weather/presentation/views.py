from rest_framework import views, status
from rest_framework.response import Response
from core.weather.presentation.serializers import WeatherModelSerializer, WeatherFillSerializer
from core.weather.services import WeatherService

class WeatherAPIView(views.APIView):
    def post(self, request, *args, **kwargs):
        serializer = WeatherFillSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        weather = WeatherService.fill_climate(data.get("start"), data.get("end"))
        return Response(weather, status=status.HTTP_200_OK)