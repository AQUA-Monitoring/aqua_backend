from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from core.users.serializers.auth_serializer import (
    AppTokenRefreshSerializer,
    EmailTokenObtainPairSerializer,
)
from core.users.serializers.sync_auth import SyncTokenSerializer

class EmailTokenObtainPairView(TokenObtainPairView):
    serializer_class = EmailTokenObtainPairSerializer


class AppTokenRefreshView(TokenRefreshView):
    serializer_class = AppTokenRefreshSerializer


class SyncTokenView(APIView):
    """Issue the restricted token used by the controlled data synchronizer."""

    authentication_classes = []
    permission_classes = []

    def post(self, request):
        serializer = SyncTokenSerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except AuthenticationFailed as exc:
            return Response({"detail": str(exc.detail)}, status=status.HTTP_401_UNAUTHORIZED)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)
