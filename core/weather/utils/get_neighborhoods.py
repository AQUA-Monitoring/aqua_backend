import os
import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon

'''
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
file = os.path.join(BASE_DIR, "fixtures", "neighborhoods.geojson")
neighborhoods = gpd.read_file(file)
'''


def get_neighborhoods(neighborhoods):
    itens = []

    for idx, nb in neighborhoods.iterrows():
        neighborhood = nb.get("bairro") or nb.get("name")

        if not neighborhood:
            print(f"Bairro não encontrado em {idx}")
            continue

        geom_nb = nb["geometry"]

        if isinstance(geom_nb, Polygon):
            geoms_nb = [geom_nb]
        elif isinstance(geom_nb, MultiPolygon):
            geoms_nb = list(geom_nb.geoms)
        else:
            print(f"Geometria incorreta para {neighborhood}")
            continue

        for geom in geoms_nb:
            itens.append({
                "name": neighborhood,
                "geometry": geom,
            })

    return itens