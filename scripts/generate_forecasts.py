from datetime import date, timedelta
import random

from core.forecast.models import Forecast

Forecast.objects.all().delete()

clusters = [
    (-26.3042, -48.8487),
    (-26.3020, -48.8535),
    (-26.2970, -48.8410),
    (-26.2873, -48.8158),
    (-26.2551, -48.8562),
    (-26.2720, -48.8725),
    (-26.3305, -48.8395),
    (-26.3488, -48.8451),
    (-26.3550, -48.8620),
    (-26.3120, -48.8850),
    (-26.2384, -48.8438),
    (-26.2943, -48.8194),
]

start = date(2026, 6, 10)

objects = []

for center_lat, center_lon in clusters:
    for _ in range(40):

        lat = round(center_lat + random.uniform(-0.003, 0.003), 8)
        lon = round(center_lon + random.uniform(-0.003, 0.003), 8)

        for day in range(30):

            current = start + timedelta(days=day)

            if day in (2, 6, 7, 14, 15, 21, 22, 27):
                p = random.uniform(0.80, 0.999999)
            else:
                r = random.random()

                if r < 0.15:
                    p = random.uniform(0.45, 0.79)
                elif r < 0.35:
                    p = random.uniform(0.20, 0.45)
                else:
                    p = random.uniform(0.0001, 0.18)

            p = round(p, 6)

            objects.append(
                Forecast(
                    latitude=lat,
                    longitude=lon,
                    date=current,
                    flood=p,
                    probability=p,
                )
            )

Forecast.objects.bulk_create(objects, batch_size=1000)

print(f"{len(objects)} registros inseridos.")