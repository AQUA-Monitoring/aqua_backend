from django.contrib import admin
from django.db import transaction
from core.flood_point_registering.infra.models import Flood_Point_Register
from core.flood_point_registering.services import sync_flood_point_neighborhoods


@admin.register(Flood_Point_Register)
class Flood_Points(admin.ModelAdmin):
    list_display = ("neighborhood", "created_at", "finished_at", "possibility")
    search_fields = ("neighborhood",)

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # O banco garante no maximo um principal; o servico garante tambem a
        # existencia dele e normaliza o FK legado em uma unica transacao.
        sync_flood_point_neighborhoods(obj)
