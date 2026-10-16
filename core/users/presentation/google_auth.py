from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from core.users.service import UsersService
from core.users.service.google_auth import GoogleLoginError, authenticate_google, configuration


class GoogleAuthThrottle(AnonRateThrottle):
    rate = "30/min"
    scope = "google_auth"


class GoogleCredentialSerializer(serializers.Serializer):
    credential = serializers.CharField(max_length=8192, trim_whitespace=False)
    challenge = serializers.CharField(max_length=1024, trim_whitespace=False)


class GoogleAuthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [GoogleAuthThrottle]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response


class GoogleConfigView(GoogleAuthView):
    def get(self, request):
        return Response(configuration())


class GoogleLoginView(GoogleAuthView):
    def post(self, request):
        serializer = GoogleCredentialSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = authenticate_google(**serializer.validated_data)
        except GoogleLoginError as exc:
            return Response({"code": exc.code, "detail": exc.detail}, status=exc.status)
        service = UsersService()
        return Response({"user": service.me_payload(user), "tokens": service.generate_tokens_for_user(user)})
