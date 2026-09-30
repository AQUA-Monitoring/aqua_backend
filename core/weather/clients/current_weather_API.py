import requests

def current_weather_API(lat, lon):
    url = 'https://api.open-meteo.com/v1/forecast'
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join([
            "temperature_2m",
            "relative_humidity_2m",
            "surface_pressure",
            "precipitation",
            "rain"
        ]),
        "forecast_days": 1,
        "timezone": "America/Sao_Paulo"
    }

    response = requests.get(url, params=params)
    data = response.json()
    
    return data