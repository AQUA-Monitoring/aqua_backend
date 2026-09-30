import os
import geopandas
from shapely.geometry import Point

from core.weather.utils.get_neighborhoods import get_neighborhoods


def process_coordinates():
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    geojson = os.path.join(BASE_DIR, "weather", "fixtures", "neighborhoods.geojson")
    step = 0.01

    gdf = geopandas.read_file(geojson)
    bairros = get_neighborhoods(gdf)
    coords = []

    for item in bairros:
        neighborhood = item["name"]
        poligono = item["geometry"]
        lon_min, lat_min, lon_max, lat_max = poligono.bounds

        lat = float(lat_min)
        while lat < float(lat_max):
            lon = float(lon_min)
            while lon < float(lon_max):
                ponto = Point(lon, lat)
                if poligono.contains(ponto):
                    coords.append({
                        "latitude": lat,
                        "longitude": lon,
                        "neighborhood": neighborhood,
                    })
                lon += step
            lat += step

    return coords