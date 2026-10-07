from django.db import models
from pathlib import Path
import os, geopandas as gpd
#from core.addressing.models import Neighborhood

def get_neighborhood():
    base_dir = Path(__file__).resolve().parents[1]
    file = base_dir / "weather" / "fixtures" / "neighborhoods.geojson"

    choices = []
    neighborhoods = gpd.read_file(file)
    for _, nb in neighborhoods.iterrows():
        if "bairro" in nb:
            neighborhood = nb["bairro"]
            choices.append((neighborhood, neighborhood))
    return choices

class Occurrence(models.Model):
    datetime = models.DateTimeField()

    class Situation(models.IntegerChoices):
        ALERTA = 1, "Alerta"
        ATENCAO = 2, "Atenção"
        EMERGENCIA = 3, "Emergência"
        NORMALIDADE = 4, "Normalidade"
    situation = models.IntegerField(choices=Situation.choices, default=Situation.NORMALIDADE)

    class Type(models.IntegerChoices):
        ALAGAMENTO = 1, "Alagamento"
        CHUVAS_INTENSAS = 2, "Chuvas Intensas"
        ENXURRADA = 3, "Enxurrada"
        INUNDACAO = 4, "Inundação"
        VENDAVAL = 5, "Vendaval"
        GRANIZO = 6, "Granizo"
        EROSAO_MARGEM_FLUVIAL = 7, "Erosão de Margem Fluvial"
        COLAPSO_EDIFICACAO = 8, "Colapso de Edificação"
        DOENCAS_INFECCIOSAS_VIRAIS = 9, "Doenças Infecciosas Virais"
        TRANSPORTE_PRODUTOS_PERIGOSOS = 10, "Transporte de Produtos Perigosos Rodoviário"
    type = models.IntegerField(choices=Type.choices, default=Type.ALAGAMENTO)
    neighborhood = models.CharField(max_length=22, choices=get_neighborhood(), null=True)
    #neighborhood = models.ManyToManyField(Neighborhood, related_name="occurrences")

    def __str__(self):
        return f'{self.situation} - {self.datetime} - {self.neighborhood}'