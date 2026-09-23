from datetime import timedelta

from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import AccessToken

from core.users.serializers.auth import SYNC_AUTH_MODEL, SYNC_SCOPE
from core.users.infra.models import User


SYNC_TOKEN_SECONDS = 3600
SYNC_TOKEN_LIFETIME = timedelta(seconds=SYNC_TOKEN_SECONDS)
INVALID_CREDENTIALS = "Credenciais inválidas"


class SyncTokenSerializer(serializers.Serializer):
    email = serializers.EmailField(write_only=True)
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        email = attrs["email"].strip()
        password = attrs["password"]
        users = User.objects.filter(email__iexact=email)

        if users.count() != 1:
            raise AuthenticationFailed(INVALID_CREDENTIALS)

        user = users.get()
        if (
            not user.check_password(password)
            or not user.is_active
            or not user.is_superuser
        ):
            raise AuthenticationFailed(INVALID_CREDENTIALS)

        token = AccessToken()
        token.set_exp(lifetime=SYNC_TOKEN_LIFETIME)
        token["user_id"] = str(user.pk)
        token["scope"] = SYNC_SCOPE
        token["auth_model"] = SYNC_AUTH_MODEL
        return {
            "access": str(token),
            "token_type": "Bearer",
            "expires_in": SYNC_TOKEN_SECONDS,
        }
