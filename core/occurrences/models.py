from django.db import models
from core.addressing.models import Neighborhood

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

    neighborhood = models.ManyToManyField(Neighborhood, related_name="occurrences")

    def __str__(self):
        return f'{self.situation} - {self.datetime} - {self.neighborhood}'