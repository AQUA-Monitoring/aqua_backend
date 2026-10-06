from core.forecast.models import Forecast
from core.forecast.services import ForecastRepoImpl
from core.weather.models import Weather
from core.occurrences.models import Occurrence
import pandas as pd

def test_dataset(repo):
    occurrence_qs = (
        Occurrence.objects
        .filter(type__in=[
            Occurrence.Type.ALAGAMENTO,
            Occurrence.Type.ENXURRADA,
            Occurrence.Type.INUNDACAO,
        ])
        .values("datetime", "neighborhood")
    )
    occurrences = pd.DataFrame(list(occurrence_qs))
    occurrences = occurrences.rename(columns={"neighborhood": "neighborhood"})
    occurrences["datetime"] = occurrences["datetime"].dt.floor("h")
    conditions = []
    coords = repo.getCoords()
    print("Coords: ", coords)
    for coord in coords:
        weather = repo.getWeatherByCoord(coord["latitude"], coord["longitude"])
        for data in weather:
            if None not in (
                data.latitude, data.longitude, data.neighborhood, data.datetime, data.rain, data.temperature, data.humidity, data.elevation, data.pressure
            ):
                conditions.append([
                    data.latitude, 
                    data.longitude,
                    data.neighborhood, 
                    data.datetime,
                    data.rain, 
                    data.temperature, 
                    data.humidity, 
                    data.elevation, 
                    data.pressure
                ])

    print("Conditions: ", len(conditions))
    df_weather = pd.DataFrame(conditions, columns=["latitude", "longitude", "neighborhood", "datetime", "rain", "temperature", "humidity", "elevation", "pressure"])

    df = pd.merge(
        df_weather,
        occurrences,
        on=["neighborhood", "datetime"],
        how="left",
        indicator=True,
    )

    matched = df[df["_merge"] == "both"]

    print(
        matched[["neighborhood", "datetime"]]
        .drop_duplicates()
        .shape[0]
    )

    print(
        occurrences[["neighborhood", "datetime"]]
        .drop_duplicates()
        .shape[0]
    )

    print(df["_merge"].value_counts())

test_dataset(ForecastRepoImpl())