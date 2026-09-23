from django.conf import settings
from rest_framework import serializers
from rest_framework.exceptions import APIException

from core.uploader.helpers.files import VIDEO_CONTENT_TYPES, get_content_type
from core.uploader.models import Video

ABSOLUTE_VIDEO_MAX_BYTES = 300 * 1024 * 1024


class VideoTooLarge(APIException):
    status_code = 413
    default_detail = "Video exceeds the 300 MiB limit."
    default_code = "video_too_large"


class UnsupportedVideoType(APIException):
    status_code = 415
    default_detail = "Invalid or unsupported video."
    default_code = "unsupported_video_type"


class VideoUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Video
        fields = ["attachment_key", "file", "description", "uploaded_on", "url"]
        read_only_fields = ["attachment_key", "uploaded_on", "url"]
        extra_kwargs = {"file": {"write_only": True}}

    def validate_file(self, value):
        max_bytes = min(settings.UPLOADER_VIDEO_MAX_BYTES, ABSOLUTE_VIDEO_MAX_BYTES)
        if value.size > max_bytes:
            raise VideoTooLarge(f"Video exceeds the limit of {max_bytes} bytes.")
        if get_content_type(value) not in VIDEO_CONTENT_TYPES:
            raise UnsupportedVideoType()
        return value


class VideoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Video
        fields = ["attachment_key", "url", "description", "uploaded_on"]
        read_only_fields = ["attachment_key", "url", "uploaded_on"]

    def create(self, validated_data):
        raise NotImplementedError("Use VideoUploadSerializer to create videos.")
