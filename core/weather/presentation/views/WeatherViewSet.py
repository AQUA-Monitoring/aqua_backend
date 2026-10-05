from rest_framework.viewsets import ModelViewSet
from rest_framework.decorators import action
from core.weather.models import Weather
from core.weather.presentation.serializers.WeatherModelSerializer import (
    WeatherModelSerializer,
)
from core.weather.services import WeatherRepositoryImpl, WeatherService
from rest_framework.response import Response
from datetime import datetime


class WeatherViewSet(ModelViewSet):
    queryset = Weather.objects.all()
    serializer_class = WeatherModelSerializer

    @action(detail=False, methods=["get"])
    def toFill(self, request):
        lat = float(request.query_params.get("lat"))
        lon = float(request.query_params.get("lon"))
        start = request.query_params.get("start")
        end = request.query_params.get("end")

        service = WeatherService(
            repository=WeatherRepositoryImpl()
        )  # Escolhe qual caso de uso utilizar
        result = service.execute(lat, lon, start, end)

        return Response(
            {"status": "Dados climáticos preenchidos com sucesso.", "dados": result}
        )

    @action(detail=False, methods=["post"], url_path="search")
    def search(self, request):
        try:
            start = "2025-08-25"
            end = datetime.now().strftime("%Y-%m-%d")
            from core.weather.infra.services.queue import enqueueFillClimates

            total_tasks, _ = enqueueFillClimates(start, end)
            return Response(
                {"status": f"{total_tasks} tarefas enfileiradas com sucesso."}
            )
        except Exception as e:
            print("❌ Erro ao processar ClimaSearch:", str(e))
            return Response({"erro": "Ocorreu um erro interno."}, status=500)
