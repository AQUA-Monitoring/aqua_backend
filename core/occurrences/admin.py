from django.contrib import admin
from core.occurrences.models import Occurrence

@admin.register(Occurrence)
class Occurrence(admin.ModelAdmin):
    list_display = ('datetime',)
    #search_fields = ("neighborhood",)