from datetime import datetime

from celery import shared_task

from core.weather.models import Weather
from core.weather.utils import process_weather


@shared_task
def fill_weather(lat, lon, neighborhood, start, end):
    climates = []
    results = process_weather(lat, lon, start, end)
    try:
        print("process_weather results keys:", [type(r) for r in results])
    except Exception:
        pass

    elevation_value = None
    for r in results:
        if isinstance(r, dict) and 'results' in r and isinstance(r['results'], list) and r['results']:
            elevation_value = r['results'][0].get('elevation')
            break

    for result in results:
        if not isinstance(result, dict) or 'hourly' not in result:   
            continue

        hourly = result.get("hourly") or {}
        times = hourly.get("time") or []
        temperatures = hourly.get("temperature_2m") or []
        humidity = hourly.get("relative_humidity_2m") or []
        pressure = hourly.get("surface_pressure") or []
        rain = hourly.get("rain") or []
        precipitation = hourly.get("precipitation") or []

        for i, value in enumerate(times):
            try:
                date_value = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                continue

            climates.append(
                Weather(
                    datetime=date_value,
                    neighborhood=neighborhood,
                    latitude=float(lat),
                    longitude=float(lon),
                    rain=rain[i] if i < len(rain) else None,
                    precipitation=precipitation[i] if i < len(precipitation) else None,
                    temperature=temperatures[i] if i < len(temperatures) else None,
                    humidity=humidity[i] if i < len(humidity) else None,
                    elevation=elevation_value,
                    pressure=pressure[i] if i < len(pressure) else None,
                    river_discharge=precipitation[i] if i < len(precipitation) else None,
                )
            )

    if not climates:
        return []

    Weather.objects.bulk_create(climates)
    return {"created": len(climates), "neighborhood": neighborhood}