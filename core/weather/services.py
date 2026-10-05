from core.weather.models import Weather
from core.weather.infra.services.weather import fillClimate as fillClimateService, fillElevation, fillFutureClimate, fillFlood
from datetime import date, timedelta

today = date.today()
forecast_start = today - timedelta(days=3)
forecast_end = today + timedelta(days=7)

class WeatherRepositoryImpl:
    def fillAll(self, lat, lon, neighborhood, start, end):
        weather = self.fillWeather(lat, lon, start, min(end, today))
        future = self.fillFutureWeather(lat, lon)
        flood = self.fillFlood(lat, lon)
        elevation = self.fillElevation(lat, lon)
        climates = []

        all_days = weather["days"] + future["days"]
        for i, day in enumerate(all_days):
            if i > len(weather["days"]):
                source = future
                idx = i - len(weather["days"])
            else:
                source = weather
                idx = i

        for i in range(len(weather["days"])): # para cada dia
            climates.append(
                Weather(
                    date=day,
                    neighborhood=neighborhood,
                    latitude=lat,
                    longitude=lon,
                    rain=source.get("rain", [None]*len(weather["days"]))[i] or future.get("rain", [None]*len(future["days"]))[i] or 0, # se não tiver "rain", retorne None para cada dia
                    temperature=source.get("temperature", [None]*len(source["days"]))[i] or future.get("temperature", [None]*len(future["days"]))[i] or 0,
                    humidity=source.get("humidity", [None]*len(source["days"]))[i] or future.get("humidity", [None]*len(future["days"]))[i] or 0,
                    elevation=elevation.get("elevation"),
                    pressure=source.get("pressure", [None]*len(weather["days"]))[i] or future.get("pressure", [None]*len(future["days"]))[i] or 0,
                    river_discharge=flood.get("river_discharge", [None]*len(weather["days"]))[i] or 0
                )
            )
        
        Weather.objects.bulk_create(climates, ignore_conflicts=True)

        return {
            "days": all_days,
            "rain": weather.get("rain", []),
            "temp": weather.get("temperature", []),
            "humidity": weather.get("humidity", []),
            "pressure": weather.get("pressure", []),
            "elevation": elevation["elevation"],
            "river_discharge": flood["river_discharge"]
        }

    def fillWeather(self, lat, lon, start, end):
        return fillClimateService(lat, lon, start, end)
    
    def fillFutureWeather(self, lat, lon):
        return fillFutureClimate(lat, lon, forecast_start, forecast_end)
    
    def fillFlood(self, lat, lon):
        return fillFlood(lat, lon)

    def fillElevation(self, lat, lon):
        return fillElevation(lat, lon)


class WeatherService:
    def __init__(self, repository: WeatherRepositoryImpl):
        self.repository = repository

    def execute(self, lat: float, lon: float, neighborhood: str, start: str, end: str):
        all_weather = self.repository.fillAll(lat, lon, neighborhood, start, end)
        return {
            "days": all_weather["days"],
            "rain": all_weather["rain"],
            "temperature": all_weather["temp"],
            "humidity": all_weather["humidity"],
            "pressure": all_weather["pressure"],
            "elevation": all_weather["elevation"],
        }
