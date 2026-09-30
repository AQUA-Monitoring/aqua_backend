from core.weather.clients import current_weather_API, elevation_API, future_weather_API


def process_weather(lat, lon, start, end):
    results = []
    current = current_weather_API(lat, lon)
    if current:
        results.append(current)
    future = future_weather_API(lat, lon, start, end)
    if future:
        results.append(future)
    elevation = elevation_API(lat, lon)
    if elevation:
        results.append(elevation)
    return results