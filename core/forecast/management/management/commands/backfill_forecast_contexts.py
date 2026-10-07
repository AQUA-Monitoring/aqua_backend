from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand

from core.forecast.services import (
    POSITIVE_WINDOW_HOURS,
    epoch_ns,
    occurrences_frame,
    flood_primary_coords,
)
from core.weather.models import Weather
from core.weather.presentation.tasks import fill_weather

from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")
ROLLING_MARGIN_HOURS = 96
CHUNK_HOURS = 24 * 330
NS = 10 ** 9
HOUR_NS = 3600 * NS


def _ns(dt):
    return int(dt.timestamp() * NS)


class Command(BaseCommand):
    help = "Backfill horario dos contextos climaticos ausentes nas janelas de ocorrencia (tipos 1-7)."

    def handle(self, *args, **options):
        occ = occurrences_frame()
        if occ.empty:
            self.stdout.write("Nenhuma ocorrencia a cobrir.")
            return

        primary = flood_primary_coords()
        self.stdout.write(f"Neighborhoods com clima: {len(primary)}")

        inserted = 0
        for nb, t in sorted(zip(occ["neighborhood"], occ["datetime"])):
            lat_lon = primary.get(nb) or self._centroid(nb)
            if lat_lon is None:
                self.stdout.write(self.style.WARNING(f"Sem coordenada para {nb}; pulando."))
                continue
            gaps = self._window_gaps(nb, lat_lon[0], lat_lon[1], t)
            for s, e in gaps:
                inserted += self._fetch(nb, lat_lon[0], lat_lon[1], s, e)
        self.stdout.write(self.style.SUCCESS(f"Backfill concluido. Linhas inseridas: {inserted}"))

    def _window_gaps(self, nb, lat, lon, t):
        s = _ns(t) - ROLLING_MARGIN_HOURS * HOUR_NS
        e = _ns(t) + POSITIVE_WINDOW_HOURS * HOUR_NS
        expected = set(map(int, np.arange(s, e, HOUR_NS)))
        if not expected:
            return []

        existing = list(
            Weather.objects.filter(
                neighborhood=nb, latitude=lat, longitude=lon,
                datetime__gte=datetime.fromtimestamp(s / NS, tz=timezone.utc),
                datetime__lt=datetime.fromtimestamp(e / NS, tz=timezone.utc),
            ).values_list("datetime", flat=True)
        )
        present = set(map(int, epoch_ns(pd.Series(existing)))) if existing else set()

        gaps = []
        missing = sorted(expected - present)
        if not missing:
            return gaps
        start = missing[0]
        prev = start
        for hour in missing[1:]:
            if hour != prev + HOUR_NS:
                gaps.append((start, prev + HOUR_NS))
                start = hour
            prev = hour
        gaps.append((start, prev + HOUR_NS))
        return gaps

    def _fetch(self, nb, lat, lon, s_ns, e_ns):
        s = datetime.fromtimestamp(s_ns / NS, tz=timezone.utc).astimezone(TZ)
        e = datetime.fromtimestamp(e_ns / NS, tz=timezone.utc).astimezone(TZ)
        chunk_start = s
        inserted = 0
        while chunk_start < e:
            chunk_end = chunk_start + timedelta(hours=CHUNK_HOURS)
            chunk_end = min(chunk_end, e)
            result = fill_weather.run(lat, lon, nb, chunk_start.date(), chunk_end.date())
            if isinstance(result, dict):
                inserted += result.get("created", 0)
            chunk_start = chunk_end
        self.stdout.write(f"  {nb}: +{inserted} linhas ({s.date()} -> {e.date()})")
        return inserted

    def _centroid(self, nb):
        import geopandas as gpd
        geo = settings.BASE_DIR / "core" / "weather" / "fixtures" / "neighborhoods.geojson"
        if not geo.exists():
            return None
        gdf = gpd.read_file(geo)
        row = gdf[gdf["bairro"] == nb]
        if row.empty:
            return None
        c = row.geometry.centroid.iloc[0]
        return (float(c.y), float(c.x))