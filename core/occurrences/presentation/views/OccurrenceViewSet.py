from rest_framework.viewsets import ModelViewSet
from core.occurrences.models import Occurrence
from core.occurrences.presentation.serializers.OccurrenceSerializer import OccurrenceSerializer

class OccurrenceViewSet(ModelViewSet):
    queryset = Occurrence.objects.all()
    serializer_class = OccurrenceSerializer