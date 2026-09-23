from django.conf import settings
from PIL import Image as PillowImage
from PIL import UnidentifiedImageError
from rest_framework import serializers

from core.uploader.helpers.files import (
    CONTENT_TYPE_JPG,
    CONTENT_TYPE_PNG,
    SVG_XML_CONTENT_TYPES,
    get_content_type,
    validate_safe_svg,
)
from core.uploader.models import Image


def validate_image_file(value):
    max_bytes = settings.UPLOADER_IMAGE_MAX_BYTES
    if value.size > max_bytes:
        raise serializers.ValidationError(
            f"Image exceeds the limit of {max_bytes} bytes."
        )

    content_type = get_content_type(value)
    if content_type in SVG_XML_CONTENT_TYPES:
        try:
            validate_safe_svg(value, max_bytes)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value

    if content_type not in {CONTENT_TYPE_JPG, CONTENT_TYPE_PNG}:
        raise serializers.ValidationError("Invalid or corrupted image.")

    try:
        image = PillowImage.open(value)
        image.verify()
    except (
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        PillowImage.DecompressionBombError,
    ) as exc:
        raise serializers.ValidationError("Invalid or corrupted image.") from exc
    finally:
        value.seek(0)
    return value


class ImageUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Image
        fields = ["attachment_key", "file", "description", "uploaded_on", "url"]
        read_only_fields = ["attachment_key", "uploaded_on", "url"]
        extra_kwargs = {"file": {"write_only": True}}

    def validate_file(self, value):
        return validate_image_file(value)


class ImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Image
        fields = ["attachment_key", "url", "description", "uploaded_on"]
        read_only_fields = ["url", "attachment_key", "uploaded_on"]

    def create(self, validated_data):
        raise NotImplementedError("Use ImageUploadSerializer to create images.")
