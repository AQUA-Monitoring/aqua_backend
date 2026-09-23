from rest_framework.permissions import BasePermission

from core.users.infra.models import User
from core.users.serializers.auth import SYNC_AUTH_MODEL, SYNC_SCOPE


class IsSyncSuperuser(BasePermission):
    message = "Token de sincronização inválido."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        token = getattr(request, "auth", None)
        return bool(
            user
            and getattr(user, "is_authenticated", False)
            and getattr(user, "is_active", False)
            and getattr(user, "is_superuser", False)
            and token
            and token.get("scope") == SYNC_SCOPE
            and token.get("auth_model") == SYNC_AUTH_MODEL
        )


class IsAppAdmin(BasePermission):
    """Allow only authenticated users whose application type is ADMIN."""

    message = "Apenas administradores podem alterar a transmissão demo."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        return bool(
            user
            and getattr(user, "is_authenticated", False)
            and (
                getattr(user, "type", None) == User.UserType.ADMIN
                or getattr(user, "is_staff", False)
                or getattr(user, "is_superuser", False)
            )
        )


class IsActiveSuperuser(BasePermission):
    """Restrict sensitive operations to active Django superusers."""

    message = "Apenas superusuários ativos podem enviar vídeos."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        return bool(
            user
            and getattr(user, "is_authenticated", False)
            and getattr(user, "is_active", False)
            and getattr(user, "is_superuser", False)
        )
