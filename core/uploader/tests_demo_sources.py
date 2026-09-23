import shutil
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from core.uploader.models import DemoVideoSource, Video
from core.users.infra.models import User
from core.users.serializers.user import UserSerializer


@override_settings(UPLOADER_VIDEO_MAX_BYTES=500 * 1024 * 1024)
class DemoSourceApiTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.media_override = override_settings(MEDIA_ROOT=self.media_root)
        self.media_override.enable()
        self.client = APIClient()
        self.app_admin = User.objects.create(
            name="Admin", email="app-admin@example.test", type=User.UserType.ADMIN
        )
        self.superuser = User.objects.create(
            name="Super", email="super@example.test", is_superuser=True, is_staff=True
        )

    def tearDown(self):
        self.media_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_admin_can_list_the_three_slots(self):
        self.client.force_authenticate(self.app_admin)
        response = self.client.get("/api/flood_monitoring/demo/sources")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {item["mode"] for item in response.data["results"]},
            {"auto", "normal", "flooded"},
        )

    @patch("core.uploader.serializers.video.get_content_type", return_value="video/mp4")
    def test_app_admin_cannot_upload_without_being_superuser(self, _content_type):
        self.client.force_authenticate(self.app_admin)
        response = self.client.put(
            "/api/flood_monitoring/demo/sources/normal",
            {
                "file": SimpleUploadedFile(
                    "normal.mp4", b"video", content_type="video/mp4"
                )
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch(
        "core.flood_camera_monitoring.presentation.demo_source_views."
        "prepare_demo_source_task.delay"
    )
    @patch("core.uploader.models.video.get_content_type", return_value="video/mp4")
    @patch("core.uploader.serializers.video.get_content_type", return_value="video/mp4")
    def test_superuser_upload_returns_processing_and_retains_old_video(
        self, _serializer_type, _model_type, delay
    ):
        self.client.force_authenticate(self.superuser)
        url = "/api/flood_monitoring/demo/sources/flooded"
        first = self.client.put(
            url,
            {
                "file": SimpleUploadedFile(
                    "first.mp4", b"first", content_type="video/mp4"
                )
            },
            format="multipart",
        )
        slot = DemoVideoSource.objects.get(mode="flooded")
        slot.status = DemoVideoSource.Status.READY
        slot.save(update_fields=["status"])
        second = self.client.put(
            url,
            {
                "file": SimpleUploadedFile(
                    "second.mp4", b"second", content_type="video/mp4"
                )
            },
            format="multipart",
        )
        self.assertEqual(first.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(second.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(second.data["status"], "processing")
        self.assertEqual(
            set(second.data),
            {
                "mode",
                "description",
                "status",
                "size_bytes",
                "uploaded_on",
                "active",
                "error",
            },
        )
        self.assertEqual(Video.objects.count(), 2)
        self.assertEqual(
            DemoVideoSource.objects.get(mode="flooded").updated_by,
            self.superuser,
        )
        self.assertEqual(
            str(DemoVideoSource.objects.get(mode="flooded").video.attachment_key),
            delay.call_args.args[1],
        )
        self.assertEqual(delay.call_count, 2)

    @patch(
        "core.flood_camera_monitoring.presentation.demo_source_views."
        "prepare_demo_source_task.delay"
    )
    @patch("core.uploader.models.video.get_content_type", return_value="video/mp4")
    @patch("core.uploader.serializers.video.get_content_type", return_value="video/mp4")
    def test_rejects_replacement_while_slot_is_processing(
        self, _serializer_type, _model_type, delay
    ):
        self.client.force_authenticate(self.superuser)
        url = "/api/flood_monitoring/demo/sources/normal"
        response = self.client.put(
            url,
            {"file": SimpleUploadedFile("one.mp4", b"one", content_type="video/mp4")},
            format="multipart",
        )
        conflict = self.client.put(
            url,
            {"file": SimpleUploadedFile("two.mp4", b"two", content_type="video/mp4")},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(conflict.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(delay.call_count, 1)

    @patch(
        "core.flood_camera_monitoring.presentation.demo_source_views."
        "prepare_demo_source_task.delay",
        side_effect=RuntimeError("queue down"),
    )
    @patch("core.uploader.models.video.get_content_type", return_value="video/mp4")
    @patch("core.uploader.serializers.video.get_content_type", return_value="video/mp4")
    def test_queue_failure_marks_slot_error_and_returns_503(
        self, _serializer_type, _model_type, _delay
    ):
        self.client.force_authenticate(self.superuser)
        response = self.client.put(
            "/api/flood_monitoring/demo/sources/auto",
            {"file": SimpleUploadedFile("auto.mp4", b"video", content_type="video/mp4")},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        slot = DemoVideoSource.objects.get(mode="auto")
        self.assertEqual(slot.status, DemoVideoSource.Status.ERROR)
        self.assertIn("indisponível", slot.error)

    @patch(
        "core.flood_camera_monitoring.infra.demo_stream_client."
        "DemoStreamClient.prepare_source"
    )
    @patch("core.uploader.models.video.get_content_type", return_value="video/mp4")
    def test_preparation_task_marks_source_ready_and_active(
        self, _model_type, prepare_source
    ):
        video = Video.objects.create(
            file=SimpleUploadedFile("normal.mp4", b"video", content_type="video/mp4")
        )
        slot, _ = DemoVideoSource.objects.get_or_create(mode="normal")
        slot.video = video
        slot.status = DemoVideoSource.Status.PROCESSING
        slot.save(update_fields=["video", "status"])
        prepare_source.return_value = {
            "demo_state": "normal",
            "sources": {"normal": {"status": "ready"}},
        }

        from core.uploader.tasks import prepare_demo_source_task

        prepare_demo_source_task.run("normal", str(video.attachment_key))

        slot.refresh_from_db()
        self.assertEqual(slot.status, DemoVideoSource.Status.READY)
        self.assertTrue(slot.active)

    def test_inactive_superuser_cannot_upload(self):
        self.superuser.is_active = False
        self.superuser.save(update_fields=["is_active"])
        self.client.force_authenticate(self.superuser)
        response = self.client.put(
            "/api/flood_monitoring/demo/sources/auto", {}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_authenticated_user_payload_exposes_superuser_flag(self):
        self.assertTrue(UserSerializer(self.superuser).data["is_superuser"])
        self.assertFalse(UserSerializer(self.app_admin).data["is_superuser"])


class VideoGlobalLimitTests(TestCase):
    @override_settings(UPLOADER_VIDEO_MAX_BYTES=500 * 1024 * 1024)
    @patch("core.uploader.serializers.video.get_content_type", return_value="video/mp4")
    def test_rejects_video_above_absolute_300_mib_limit(self, _content_type):
        upload = SimpleUploadedFile("too-large.mp4", b"x", content_type="video/mp4")
        upload.size = 300 * 1024 * 1024 + 1
        from core.uploader.serializers.video import VideoUploadSerializer

        serializer = VideoUploadSerializer(data={"file": upload})
        from core.uploader.serializers.video import VideoTooLarge

        with self.assertRaisesRegex(VideoTooLarge, "314572800"):
            serializer.is_valid()

    @override_settings(UPLOADER_VIDEO_MAX_BYTES=500 * 1024 * 1024)
    @patch("core.uploader.serializers.video.get_content_type", return_value="video/mp4")
    def test_accepts_video_at_exact_300_mib_limit(self, _content_type):
        upload = SimpleUploadedFile("at-limit.mp4", b"x", content_type="video/mp4")
        upload.size = 300 * 1024 * 1024
        from core.uploader.serializers.video import VideoUploadSerializer

        serializer = VideoUploadSerializer(data={"file": upload})
        self.assertTrue(serializer.is_valid(), serializer.errors)
