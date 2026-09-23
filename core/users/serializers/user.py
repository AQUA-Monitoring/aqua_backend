from rest_framework import serializers

from core.uploader.serializers.image import validate_image_file


class UserSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    profile_picture = serializers.SerializerMethodField()
    profile_picture_id = serializers.SerializerMethodField()
    is_superuser = serializers.BooleanField(read_only=True)

    @staticmethod
    def get_profile_picture(user):
        if user.profile_picture:
            return user.profile_picture.url
        return user.profile_picture_url or None

    @staticmethod
    def get_profile_picture_id(user):
        return user.profile_picture.attachment_key if user.profile_picture else None


class SignupSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    password = serializers.CharField(min_length=6, max_length=128, write_only=True)
    profile_picture = serializers.FileField(required=False, allow_null=True)
    profile_picture_id = serializers.UUIDField(required=False, allow_null=True)

    def validate_profile_picture(self, value):
        return validate_image_file(value)


class UpdateUserSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, required=False)
    email = serializers.EmailField(required=False)
    profile_picture = serializers.FileField(required=False, allow_null=True)
    profile_picture_id = serializers.UUIDField(required=False, allow_null=True)

    def validate_profile_picture(self, value):
        return validate_image_file(value)


class TokenPairSerializer(serializers.Serializer):
    access = serializers.CharField()
    refresh = serializers.CharField()
