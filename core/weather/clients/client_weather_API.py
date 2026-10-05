import requests

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PAST_URL = "https://archive-api.open-meteo.com/v1/archive"

WEATHER_VARIABLES = [
    "temperature_2m",
    "relative_humidity_2m",
    "surface_pressure",
    "precipitation",
    "rain"
]

def request_weather(lat, lon, url, **props):
    url = url
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(WEATHER_VARIABLES),
        "timezone": "America/Sao_Paulo",
        **props
    }

    response = requests.get(url, params=params)
    data = response.json()
    
    return data

def get_forecast_weather(lat, lon):
    return request_weather(lat, lon, FORECAST_URL)

def get_past_weather(lat, lon, start, end):
    return request_weather(lat, lon, PAST_URL, start_date=start, end_date=end)