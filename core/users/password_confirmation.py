from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.throttling import UserRateThrottle

from core.users.permissions import IsAppAdmin


class PasswordConfirmationSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=1024)


def require_admin_password(request):
    if not request.user.is_active or not IsAppAdmin().has_permission(request, None):
        raise PermissionDenied("Apenas administradores ativos podem realizar esta operação.")
    serializer = PasswordConfirmationSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    if not request.user.check_password(serializer.validated_data["password"]):
        raise serializers.ValidationError({"password": "Senha incorreta."})


class AdminPasswordThrottle(UserRateThrottle):
    scope = "admin_password_confirmation"
    rate = "10/min"
