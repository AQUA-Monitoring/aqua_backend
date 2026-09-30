import requests

def future_weather_API(lat, lon, start, end):
    url = 'https://api.open-meteo.com/v1/forecast'
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start,
        "end_date": end,
        "hourly": ",".join([
            "precipitation",
            "temperature_2m",
            "relative_humidity_2m",
        ])
    }

    response = requests.get(url, params=params)
    data = response.json()

    return data