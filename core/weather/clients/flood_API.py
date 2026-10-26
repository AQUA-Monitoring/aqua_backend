import requests

def flood_API(lat, lon):
    url = 'https://api.open-elevation.com/api/v1/flood'
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "river_discharge"
    }

    response = requests.get(url, params=params)
    data = response.json()

    return data