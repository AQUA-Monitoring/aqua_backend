from __future__ import annotations

from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication as SimpleJWTAuth
from core.users.infra.models import User


SYNC_SCOPE = "sync:read"
SYNC_AUTH_MODEL = "django"


class AppJWTAuthentication(SimpleJWTAuth):
    def get_user(self, validated_token):  # type: ignore[override]
        scope = validated_token.get("scope")
        auth_model = validated_token.get("auth_model")
        if scope is not None or auth_model is not None:
            if scope != SYNC_SCOPE or auth_model != SYNC_AUTH_MODEL:
                raise AuthenticationFailed(_("Invalid token"))

            user_id = validated_token.get("user_id")
            if not user_id:
                raise AuthenticationFailed(_("Invalid sync token"))
            try:
                user = User.objects.get(pk=user_id)
            except (User.DoesNotExist, ValueError, TypeError) as exc:
                raise AuthenticationFailed(_("Invalid sync token")) from exc
            if not user.is_active or not user.is_superuser:
                raise AuthenticationFailed(_("Invalid sync token"))
            return user

        # Import locally to avoid circular import during DRF settings initialization
        user_id = validated_token.get("user_id")
        if not user_id:
            raise AuthenticationFailed(_("Invalid token: no user_id"))
        try:
            return User.objects.get(id=user_id)
        except User.DoesNotExist:
            raise AuthenticationFailed(_("User not found"))
