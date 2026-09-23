from rest_framework import serializers
from core.uploader.serializers import ImageSerializer
from core.blog.infra.models import Post  

class PostSerializer(serializers.ModelSerializer):
    banner_image = ImageSerializer(required=False, allow_null=True)
    content_image = ImageSerializer(required=False, allow_null=True)

    class Meta:
        model = Post
        fields = (
            "id",
            "title",
            "subject",
            "author",
            "content",
            "banner_image",
            "content_image",
            "reference_title",
            "reference_url",
            "created_at",
        )
