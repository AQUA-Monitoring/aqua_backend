from rest_framework import mixins, parsers, permissions, viewsets

from core.uploader.models import Document, Image, Video
from core.uploader.serializers import (
    DocumentUploadSerializer,
    ImageUploadSerializer,
    VideoUploadSerializer,
)
from core.users.permissions import IsActiveSuperuser


class CreateViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    pass


class DocumentUploadViewSet(CreateViewSet):
    queryset = Document.objects.all() #  pylint: disable=no-member
    serializer_class = DocumentUploadSerializer
    parser_classes = [parsers.FormParser, parsers.MultiPartParser]


class ImageUploadViewSet(CreateViewSet):
    queryset = Image.objects.all() #  pylint: disable=no-member
    serializer_class = ImageUploadSerializer
    parser_classes = [parsers.FormParser, parsers.MultiPartParser]


class VideoUploadViewSet(CreateViewSet):
    queryset = Video.objects.all()  # pylint: disable=no-member
    serializer_class = VideoUploadSerializer
    parser_classes = [parsers.FormParser, parsers.MultiPartParser]

    def get_permissions(self):
        return [permissions.IsAuthenticated(), IsActiveSuperuser()]
