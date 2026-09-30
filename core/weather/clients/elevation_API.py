import requests

def elevation_API(lat, lon):
    url = 'https://api.open-elevation.com/api/v1/lookup'
    params = {"locations": f'{lat},{lon}'}

    response = requests.get(url, params=params)
    data = response.json()

    return data