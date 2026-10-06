from django.db import models

class Forecast(models.Model):
    latitude = models.FloatField()
    longitude = models.FloatField()
    datetime = models.DateTimeField()
    flood = models.FloatField()
    probability = models.FloatField()

    class Meta:
        unique_together = ("latitude", "longitude", "datetime")
        verbose_name_plural = "Forecasts"