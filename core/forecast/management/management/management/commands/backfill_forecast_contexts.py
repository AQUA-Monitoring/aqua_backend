from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Max, Min

from core.forecast.services import POSITIVE_WINDOW_HOURS, occurrences_frame, flood_primary_coords
from core.weather.models import Weather
from core.weather.presentation.tasks import fill_weather

from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")
ROLLING_MARGIN_HOURS = 96
CHUNK_HOURS = 24 * 330
NS = 10 ** 9


def _ns(dt):
    return int(dt.timestamp() * NS)


def merge_intervals(intervals):
    intervals = sorted(intervals)
    merged = []
    for s, e in intervals:
        if not merged or s > merged[-1][1]:
            merged.append([s, e])
        elif e > merged[-1][1]:
            merged[-1][1] = e
    return [(s, e) for s, e in merged]


class Command(BaseCommand):
    help = "Backfill dos contextos climaticos ausentes para as janelas de ocorrencia (tipos 1-7)."

    def handle(self, *args, **options):
        occ = occurrences_frame()
        if occ.empty:
            self.stdout.write("Nenhuma ocorrencia a cobrir.")
            return

        primary = flood_primary_coords()
        self.stdout.write(f"Neighborhoods com clima: {len(primary)}")

        margin = timedelta(hours=ROLLING_MARGIN_HOURS)
        inserted = 0
        for nb in sorted(occ["neighborhood"].unique()):
            times = occ.loc[occ["neighborhood"] == nb, "datetime"]
            if times.empty:
                continue
            lat_lon = primary.get(nb) or self._centroid(nb)
            if lat_lon is None:
                self.stdout.write(self.style.WARNING(f"Sem coordenada para {nb}; pulando."))
                continue
            lat, lon = lat_lon
            cov = Weather.objects.filter(neighborhood=nb, latitude=lat, longitude=lon).aggregate(
                mn=Min("datetime"), mx=Max("datetime")
            )
            ranges = self._uncovered_ranges(nb, times, cov["mn"], cov["mx"], margin)
            if not ranges:
                self.stdout.write(f"{nb}: ja coberto.")
                continue
            for s, e in ranges:
                inserted += self._fetch(nb, lat, lon, s, e)
        self.stdout.write(self.style.SUCCESS(f"Backfill concluido. Linhas inseridas: {inserted}"))

    def _uncovered_ranges(self, nb, times, cov_min, cov_max, margin):
        W = (POSITIVE_WINDOW_HOURS + ROLLING_MARGIN_HOURS) * 3600 * NS
        win_end = timedelta(hours=POSITIVE_WINDOW_HOURS)
        intervals = []
        for t in times:
            s = _ns(t - margin)
            e = _ns(t + win_end)
            intervals.append((s, e))

        ranges = []
        for s, e in merge_intervals(intervals):
            if cov_min is not None and cov_max is not None:
                c_min = _ns(cov_min)
                c_max = _ns(cov_max)
                if s >= c_min and e <= c_max:
                    continue
                if s < c_min:
                    end = min(e, c_min)
                    if s < end:
                        ranges.append((s, end))
                if e > c_max:
                    start = max(s, c_max)
                    if start < e:
                        ranges.append((start, e))
            else:
                ranges.append((s, e))
        return ranges

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