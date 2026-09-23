from rest_framework import serializers

from core.uploader.models import DemoVideoSource
from core.uploader.serializers.video import VideoUploadSerializer


class DemoSourceUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    description = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )

    def validate_file(self, value):
        serializer = VideoUploadSerializer(data={"file": value})
        serializer.is_valid(raise_exception=True)
        return value


class DemoSourceSerializer(serializers.ModelSerializer):
    description = serializers.CharField(
        source="video.description", read_only=True, default=""
    )
    size_bytes = serializers.SerializerMethodField()
    uploaded_on = serializers.DateTimeField(
        source="video.uploaded_on", read_only=True, allow_null=True
    )

    class Meta:
        model = DemoVideoSource
        fields = [
            "mode",
            "description",
            "status",
            "size_bytes",
            "uploaded_on",
            "active",
            "error",
        ]

    @staticmethod
    def get_size_bytes(obj):
        if obj.video and obj.video.file:
            try:
                return obj.video.file.size
            except OSError:
                return None
        return None
