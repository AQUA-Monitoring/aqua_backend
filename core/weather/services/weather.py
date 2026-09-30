from core.weather.utils.process_coordinates import process_coordinates
from core.weather.tasks.fill_weather import fill_weather

class WeatherService:
    @staticmethod
    def fill_climate(start, end):
        coords = process_coordinates()

        for point in coords:
            fill_weather.delay(
                point["latitude"],
                point["longitude"],
                point["neighborhood"],
                start,
                end,
            )

        return {"queued": len(coords)}