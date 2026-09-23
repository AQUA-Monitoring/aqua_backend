from django.test import TestCase
from rest_framework.test import APIClient

from core.users.infra.models import User


class TokenRefreshTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User(name="Usuário JWT", email="jwt@example.test")
        self.user.set_password("secret-test")
        self.user.save()

    def test_uuid_user_can_obtain_and_refresh_token(self):
        obtained = self.client.post(
            "/api/auth/token/",
            {"email": self.user.email, "password": "secret-test"},
            format="json",
        )
        self.assertEqual(obtained.status_code, 200)

        refreshed = self.client.post(
            "/api/auth/token/refresh/",
            {"refresh": obtained.data["refresh"]},
            format="json",
        )
        self.assertEqual(refreshed.status_code, 200)
        self.assertIn("access", refreshed.data)

    def test_sync_token_requires_active_superuser(self):
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])

        response = self.client.post(
            "/api/sync/token/",
            {"email": self.user.email, "password": "secret-test"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("access", response.data)
        self.assertEqual(response.data["token_type"], "Bearer")

    def test_sync_token_rejects_non_superuser(self):
        response = self.client.post(
            "/api/sync/token/",
            {"email": self.user.email, "password": "secret-test"},
            format="json",
        )

        self.assertEqual(response.status_code, 401, response.data)

    def test_refresh_for_removed_user_is_unauthorized(self):
        obtained = self.client.post(
            "/api/auth/token/",
            {"email": self.user.email, "password": "secret-test"},
            format="json",
        )
        refresh = obtained.data["refresh"]
        self.user.delete()

        response = self.client.post(
            "/api/auth/token/refresh/", {"refresh": refresh}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_invalid_refresh_is_unauthorized(self):
        response = self.client.post(
            "/api/auth/token/refresh/", {"refresh": "invalid"}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_me_exposes_superuser_flag(self):
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.client.force_authenticate(self.user)

        response = self.client.get("/api/users/me/")

        self.assertEqual(response.status_code, 200)
        self.assertIs(response.data["is_superuser"], True)
