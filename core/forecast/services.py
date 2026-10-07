from datetime import timedelta
import joblib, os
from pathlib import Path

import numpy as np
import pandas as pd
from django.conf import settings
from django.db.models import Count, Max
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import roc_auc_score, average_precision_score, precision_recall_curve

from core.forecast.models import Forecast
from core.weather.models import Weather
from core.occurrences.models import Occurrence

FLOOD_TYPES = [
    Occurrence.Type.ALAGAMENTO,
    Occurrence.Type.CHUVAS_INTENSAS,
    Occurrence.Type.ENXURRADA,
    Occurrence.Type.INUNDACAO,
    Occurrence.Type.VENDAVAL,
    Occurrence.Type.GRANIZO,
    Occurrence.Type.EROSAO_MARGEM_FLUVIAL,
]
POSITIVE_WINDOW_HOURS = 48
NEGATIVE_BUFFER_HOURS = 72
NEGATIVE_RATIO = 3
FEATURES = [
    "rain", "precipitation", "temperature", "humidity", "pressure", "elevation",
    "rain_6h", "rain_24h", "rain_48h", "rain_max_24h",
    "precip_24h", "precip_48h",
    "temp_24h_mean", "temp_24h_max",
    "humidity_24h_mean", "humidity_24h_min",
    "pressure_min_24h", "pressure_min_48h", "pressure_delta_24h",
    "month", "hour_sin", "hour_cos",
    "latitude", "longitude",
]
MODEL_VERSION = "rf_48h_window_v1"


class ForecastRepoImpl:
    def getCoords(self):
        return Weather.objects.values("latitude", "longitude").distinct()

    def getWeatherByCoord(self, lat, lon):
        return Weather.objects.filter(latitude=lat, longitude=lon)

    def forecast(self, lat, lon, flood, datetime, probability):
        Forecast.objects.update_or_create(
            latitude=lat,
            longitude=lon,
            flood=flood,
            datetime=datetime,
            probability=probability,
        )


def flood_primary_coords():
    rows = (
        Weather.objects.values("neighborhood", "latitude", "longitude")
        .annotate(n=Count("id"))
        .order_by("neighborhood", "-n")
    )
    primary = {}
    for r in rows:
        primary.setdefault(r["neighborhood"], (r["latitude"], r["longitude"]))
    return primary


def occurrences_frame():
    occ = list(
        Occurrence.objects.filter(
            type__in=FLOOD_TYPES, neighborhood__isnull=False
        ).values("neighborhood", "datetime")
    )
    df = pd.DataFrame(occ, columns=["neighborhood", "datetime"])
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    return df.drop_duplicates(subset=["neighborhood", "datetime"]).reset_index(drop=True)


def load_weather(nb, lat, lon):
    q = (
        Weather.objects.filter(neighborhood=nb, latitude=lat, longitude=lon)
        .order_by("datetime")
        .values(
            "datetime", "latitude", "longitude", "rain", "precipitation",
            "temperature", "humidity", "pressure", "elevation",
        )
    )
    df = pd.DataFrame(list(q))
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    return df.sort_values("datetime").reset_index(drop=True)


def compute_features(df):
    df = df.copy()
    df["rain_6h"] = df["rain"].rolling(6).sum()
    df["rain_24h"] = df["rain"].rolling(24).sum()
    df["rain_48h"] = df["rain"].rolling(48).sum()
    df["rain_max_24h"] = df["rain"].rolling(24).max()
    df["precip_24h"] = df["precipitation"].rolling(24).sum()
    df["precip_48h"] = df["precipitation"].rolling(48).sum()
    df["temp_24h_mean"] = df["temperature"].rolling(24).mean()
    df["temp_24h_max"] = df["temperature"].rolling(24).max()
    df["humidity_24h_mean"] = df["humidity"].rolling(24).mean()
    df["humidity_24h_min"] = df["humidity"].rolling(24).min()
    df["pressure_min_24h"] = df["pressure"].rolling(24).min()
    df["pressure_min_48h"] = df["pressure"].rolling(48).min()
    df["pressure_delta_24h"] = df["pressure"] - df["pressure"].shift(24)
    df["month"] = df["datetime"].dt.month
    hour = df["datetime"].dt.hour + df["datetime"].dt.minute / 60.0
    df["hour_sin"] = np.sin(2 * np.pi * hour / 24.0)
    df["hour_cos"] = np.cos(2 * np.pi * hour / 24.0)
    return df.reset_index(drop=True)


def epoch_ns(series):
    idx = pd.DatetimeIndex(series)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    return idx.values.astype("datetime64[ns]").astype("int64")


def window_labels(df, occ_ns, window_hours, buffer_hours):
    window_ns = window_hours * 3600 * 10 ** 9
    buffer_ns = buffer_hours * 3600 * 10 ** 9
    ts = epoch_ns(df["datetime"])
    labels = np.zeros(len(df), dtype=bool)
    buffer = np.zeros(len(df), dtype=bool)
    for t in occ_ns:
        lo = max(0, int(np.searchsorted(ts, t - window_ns, side="left")))
        hi = int(np.searchsorted(ts, t, side="left"))
        labels[lo:hi] = True
        blo = max(0, int(np.searchsorted(ts, t - buffer_ns, side="left")))
        bhi = int(np.searchsorted(ts, t + buffer_ns, side="right"))
        buffer[blo:min(bhi, len(buffer))] = True
    return labels, buffer


def build_dataset():
    occ = occurrences_frame()
    if occ.empty:
        return pd.DataFrame()
    primary = flood_primary_coords()
    frames = []
    for nb in sorted(occ["neighborhood"].unique()):
        if nb not in primary:
            continue
        lat, lon = primary[nb]
        series = load_weather(nb, lat, lon)
        if series.empty:
            continue
        features = compute_features(series)
        occ_ns = epoch_ns(occ.loc[occ["neighborhood"] == nb, "datetime"])
        labels, buffer = window_labels(features, occ_ns, POSITIVE_WINDOW_HOURS, NEGATIVE_BUFFER_HOURS)
        ok = features[FEATURES].notna().all(axis=1)

        positives = features[ok & labels].copy()
        positives["y"] = 1
        if positives.empty:
            continue

        candidates = features[ok & (~labels) & (~buffer)].copy()
        n_neg = min(int(NEGATIVE_RATIO * len(positives)), len(candidates))
        negatives = candidates.sample(n=n_neg, random_state=42).copy()
        negatives["y"] = 0

        out = pd.concat([positives, negatives], ignore_index=True)
        out["neighborhood"] = nb
        frames.append(out)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _model_dir():
    path = Path(settings.FORECAST_MODEL_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def best_threshold(y_true, proba):
    prec, rec, thr = precision_recall_curve(y_true, proba)
    denom = prec[:-1] + rec[:-1]
    f1 = np.divide(2 * prec[:-1] * rec[:-1], denom, out=np.zeros_like(denom), where=denom > 0)
    best = int(np.argmax(f1))
    return float(thr[best])


def train_forecast():
    df = build_dataset()
    if df.empty:
        raise RuntimeError("Dataset vazio: nenhum contexto clima/ocorrencia encontrado.")
    if df["y"].nunique() < 2:
        raise RuntimeError("Dataset sem classes suficientes (nao ha negativos).")

    df = df.sort_values("datetime").reset_index(drop=True)
    split = int(len(df) * 0.80)
    train, test = df.iloc[:split], df.iloc[split:]
    X_train, y_train = train[FEATURES], train["y"].astype(int)
    X_test, y_test = test[FEATURES], test["y"].astype(int)

    scaler = StandardScaler().fit(X_train)
    rf = RandomForestClassifier(
        n_estimators=300,
        max_depth=None,
        max_features="sqrt",
        class_weight="balanced",
        n_jobs=-1,
        random_state=42,
    )
    clf = CalibratedClassifierCV(rf, cv=min(3, max(2, y_train.value_counts().min())), method="isotonic")
    clf.fit(scaler.transform(X_train), y_train)

    proba = clf.predict_proba(scaler.transform(X_test))[:, 1]
    thr = best_threshold(y_test, proba)
    y_pred = (proba >= thr).astype(int)

    metrics = {
        "n_train": int(len(train)),
        "n_test": int(len(test)),
        "n_positive": int(df["y"].sum()),
        "n_negative": int((~df["y"].astype(bool)).sum()),
        "roc_auc": float(roc_auc_score(y_test, proba)),
        "average_precision": float(average_precision_score(y_test, proba)),
        "threshold": thr,
        "accuracy": float((y_pred == y_test).mean()),
        "flood_positive_share": float(y_test.mean()),
        "trained_at": pd.Timestamp.utcnow().isoformat(),
        "model_version": MODEL_VERSION,
    }

    path = _model_dir()
    joblib.dump(clf, path / "model.joblib")
    joblib.dump(scaler, path / "scaler.joblib")
    joblib.dump({
        "features": FEATURES,
        "threshold": thr,
        "model_version": MODEL_VERSION,
        "metrics": metrics,
    }, path / "meta.joblib")
    print(metrics)
    return metrics


def load_model():
    path = _model_dir()
    model_file = path / "model.joblib"
    if not model_file.exists():
        return None
    clf = joblib.load(model_file)
    scaler = joblib.load(path / "scaler.joblib")
    meta = joblib.load(path / "meta.joblib")
    return clf, scaler, meta


def flood_lookup_inputs(df):
    ok = df[FEATURES].notna().all(axis=1)
    return df[ok]


def runForecast(repo):  # Pontua o clima recente/futuro e persiste Forecast
    artifacts = load_model()
    if artifacts is None:
        print("Nenhum artefato de modelo: treinando antes de prever.")
        train_forecast()
        artifacts = load_model()
    clf, scaler, meta = artifacts

    now = Weather.objects.aggregate(mx=Max("datetime"))["mx"]
    coords = list(repo.getCoords())
    if now is None or not coords:
        return

    cutoff = now - timedelta(days=7)
    start = cutoff - timedelta(hours=120)

    records = []
    threshold = meta["threshold"]
    for coord in coords:
        qs = (
            Weather.objects
            .filter(latitude=coord["latitude"], longitude=coord["longitude"], datetime__gte=start)
            .order_by("datetime")
            .values(
                "datetime", "latitude", "longitude", "rain", "precipitation",
                "temperature", "humidity", "pressure", "elevation",
            )
        )
        df = pd.DataFrame(list(qs))
        if df.empty:
            continue
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
        features = compute_features(df)
        features = flood_lookup_inputs(features)
        features = features[features["datetime"] >= cutoff]
        if features.empty:
            continue
        X = scaler.transform(features[FEATURES])
        proba = clf.predict_proba(X)[:, 1]
        for (_, row), p in zip(features.iterrows(), proba):
            records.append(Forecast(
                latitude=coord["latitude"],
                longitude=coord["longitude"],
                datetime=row["datetime"],
                flood=int(p >= float(threshold)),
                probability=float(p),
            ))

    if records:
        Forecast.objects.bulk_create(
            records,
            update_conflicts=True,
            update_fields=["flood", "probability"],
            unique_fields=["latitude", "longitude", "datetime"],
            batch_size=2000,
        )
    print("Forecasts:", len(records))


def floodingPredict(repo):
    runForecast(repo)