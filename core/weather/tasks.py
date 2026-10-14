"""Register nested weather tasks with Celery autodiscovery."""
from core.weather.presentation.tasks import fill_weather

__all__ = ["fill_weather"]
