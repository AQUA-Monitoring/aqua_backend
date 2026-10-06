from django.db import models

class Weather(models.Model):
    datetime = models.DateTimeField()
    latitude = models.FloatField()
    longitude = models.FloatField()
    neighborhood = models.CharField(max_length=20)
    rain = models.FloatField(null=True, blank=True)
    precipitation = models.FloatField(null=True, blank=True)
    temperature = models.FloatField(null=True, blank=True)
    humidity = models.FloatField(null=True, blank=True)
    elevation = models.FloatField(null=True, blank=True)
    pressure = models.FloatField(null=True, blank=True)
    river_discharge = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f'{self.neighborhood} - {self.datetime}'