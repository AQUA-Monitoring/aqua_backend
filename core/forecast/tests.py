import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
from django.conf import settings
from django.test import TestCase, override_settings
import pandas as pd

from core.forecast.models import Forecast
from core.forecast.services import (
    ForecastRepoImpl,
    POSITIVE_WINDOW_HOURS,
    build_dataset,
    train_forecast,
    runForecast,
)
from core.weather.models import Weather
from core.occurrences.models import Occurrence


def seed_climate(nb, lat, lon, hours=1400):
    start = datetime(2021, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(hours):
        dt = start + timedelta(hours=i)
        rain = float(max(0.0, 8 * np.sin(i / 90.0))) if (i % 120) > 100 else float((i % 7) * 0.3)
        temp = float(18 + 8 * np.sin(i / 24.0))
        rows.append(Weather(
            datetime=dt, latitude=lat, longitude=lon, neighborhood=nb,
            rain=rain, precipitation=rain, temperature=temp,
            humidity=float(70 + 15 * np.sin(i / 30.0)),
            elevation=5.0, pressure=float(1013 - rain),
        ))
    Weather.objects.bulk_create(rows)


class ForecastPipelineTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_climate("América", -26.3, -48.8)
        seed_climate("Centro", -26.31, -48.85)
        base = datetime(2021, 1, 1, tzinfo=timezone.utc)
        occ = [
            Occurrence(datetime=base + timedelta(hours=800), situation=3, type=Occurrence.Type.ALAGAMENTO, neighborhood="América"),
            Occurrence(datetime=base + timedelta(hours=960), situation=3, type=Occurrence.Type.ENXURRADA, neighborhood="América"),
            Occurrence(datetime=base + timedelta(hours=820), situation=4, type=Occurrence.Type.CHUVAS_INTENSAS, neighborhood="Centro"),
            Occurrence(datetime=base + timedelta(hours=1000), situation=3, type=Occurrence.Type.INUNDACAO, neighborhood="Centro"),
        ]
        Occurrence.objects.bulk_create(occ)

    def test_build_dataset_balanced(self):
        df = build_dataset()
        self.assertGreater(len(df), 0)
        self.assertIn("rain_48h", df.columns)
        self.assertIn("y", df.columns)
        positives = int(df["y"].sum())
        negatives = int((~df["y"].astype(bool)).sum())
        self.assertGreater(positives, 0)
        self.assertGreater(negatives, 0)
        self.assertLessEqual(negatives, 3 * positives + 5)

    def test_labels_inside_window(self):
        df = build_dataset()
        occ = list(Occurrence.objects.values_list("neighborhood", "datetime"))
        pos = df[df["y"] == 1]
        for _, row in pos.iterrows():
            near = any(
                row["neighborhood"] == nb
                and timedelta(0) <= (dt - row["datetime"]) <= timedelta(hours=POSITIVE_WINDOW_HOURS)
                for nb, dt in occ
            )
            self.assertTrue(near, f"positivo fora da janela: {row['datetime']}")

    @override_settings(FORECAST_MODEL_DIR=Path(tempfile.mkdtemp()))
    def test_train_predict_smoke(self):
        metrics = train_forecast()
        self.assertIn("roc_auc", metrics)
        self.assertTrue((settings.FORECAST_MODEL_DIR / "model.joblib").exists())
        runForecast(ForecastRepoImpl())
        self.assertGreater(Forecast.objects.count(), 0)

    def test_have_negatives(self):
        df = build_dataset()
        self.assertTrue((df["y"] == 0).any())