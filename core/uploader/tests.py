import shutil
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from core.uploader.models import Image, Video
from core.uploader.serializers import ImageUploadSerializer
from core.users.infra.models import User


class UploadSerializerTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()
        self.client = APIClient()
        self.admin = User.objects.create(
            name="Administrador",
            email="admin-upload@example.com",
            type=User.UserType.ADMIN,
            is_superuser=True,
        )
        self.client.force_authenticate(user=self.admin)

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    @patch(
        "core.uploader.serializers.image.get_content_type",
        return_value="image/svg+xml",
    )
    def test_uploads_safe_svg_as_an_image(self, _get_content_type):
        upload = SimpleUploadedFile(
            "map.svg",
            b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1z"/></svg>',
            content_type="image/svg+xml",
        )

        serializer = ImageUploadSerializer(
            data={"file": upload, "description": "Mapa vetorial"}
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        image = serializer.save()
        self.assertIsInstance(image, Image)
        self.assertTrue(image.file.name.endswith(".svg"))

    @patch(
        "core.uploader.serializers.image.get_content_type",
        return_value="image/svg+xml",
    )
    def test_rejects_executable_svg(self, _get_content_type):
        upload = SimpleUploadedFile(
            "unsafe.svg",
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            content_type="image/svg+xml",
        )

        serializer = ImageUploadSerializer(data={"file": upload})

        self.assertFalse(serializer.is_valid())
        self.assertIn("not allowed", str(serializer.errors["file"][0]))

    @patch(
        "core.uploader.serializers.video.get_content_type",
        return_value="video/mp4",
    )
    def test_uploads_video_to_its_own_collection(self, _get_content_type):
        upload = SimpleUploadedFile(
            "demo.mp4",
            b"small-video-fixture",
            content_type="video/mp4",
        )

        with patch(
            "core.uploader.models.video.get_content_type", return_value="video/mp4"
        ):
            response = self.client.post(
                "/api/upload/videos/",
                {"file": upload, "description": "Demo de alagamento"},
                format="multipart",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        video = Video.objects.get()
        self.assertIsInstance(video, Video)
        self.assertTrue(video.file.name.endswith(".mp4"))
        self.assertEqual(response.data["attachment_key"], str(video.attachment_key))
