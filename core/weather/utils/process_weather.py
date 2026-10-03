from datetime import datetime, timedelta, date
from core.weather.clients import elevation_API, get_past_weather, get_forecast_weather

def normalize_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if "T" in str(value):
        return datetime.fromisoformat(str(value)).date()
    return date.fromisoformat(str(value))

def process_weather(lat, lon, start, end):
    results = []
    
    start = normalize_date(start)
    end = normalize_date(end)
    today = date.today()

    elevation = elevation_API(lat, lon)
    if elevation:
        results.append(elevation)

    if end < today:
        past_end = min(end, today)
        past = get_past_weather(lat, lon, start, past_end)
        if past:
            results.append(past)

    elif start > today:
        forecast_end = min(end, today + timedelta(days=16))
        forecast = get_forecast_weather(lat, lon)
        if forecast:
            results.append(forecast)

    elif start == today:
        forecast_end = min(end, today + timedelta(days=16))
        forecast = get_forecast_weather(lat, lon)
        if forecast:
            results.append(forecast)

    else:
        past_end = today - timedelta(days=1)
        if past_end < start:
            past_end = start
        if past_end >= start and past_end < today:
            past = get_past_weather(lat, lon, start, past_end)
            if past:
                results.append(past)
        if end >= today:
            forecast = get_forecast_weather(lat, lon)
            if forecast:
                results.append(forecast)

    return results