from rest_framework import parsers, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.uploader.models import DemoVideoSource
from core.uploader.serializers import DemoSourceSerializer, DemoSourceUploadSerializer
from core.uploader.services import DemoSourceBusy, replace_demo_source
from core.uploader.tasks import prepare_demo_source_task
from core.users.permissions import IsActiveSuperuser, IsAppAdmin


class DemoSourcesView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAppAdmin]

    def get(self, request):
        slots = []
        for mode in DemoVideoSource.Mode.values:
            slot, _ = DemoVideoSource.objects.get_or_create(mode=mode)
            slots.append(slot)
        return Response({"results": DemoSourceSerializer(slots, many=True).data})


class DemoSourceUploadView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsActiveSuperuser]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def put(self, request, mode: str):
        if mode not in DemoVideoSource.Mode.values:
            return Response(
                {"mode": ["Modo inválido."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = DemoSourceUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            slot = replace_demo_source(
                mode=mode,
                file=serializer.validated_data["file"],
                description=serializer.validated_data.get("description", ""),
                updated_by=request.user,
            )
        except DemoSourceBusy:
            return Response(
                {"detail": "Este modo já possui um vídeo em processamento."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            prepare_demo_source_task.delay(mode, str(slot.video.attachment_key))
        except Exception as exc:
            slot.status = DemoVideoSource.Status.ERROR
            slot.error = "O processamento de vídeos está indisponível no momento."
            slot.save(update_fields=["status", "error", "updated_at"])
            return Response(
                {"detail": slot.error},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(DemoSourceSerializer(slot).data, status=status.HTTP_202_ACCEPTED)
