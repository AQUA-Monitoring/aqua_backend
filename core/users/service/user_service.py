from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rest_framework import exceptions
from rest_framework_simplejwt.tokens import RefreshToken

from core.users.infra.models import User
from core.uploader.models import Image


@dataclass
class UsersService:
    """Application service for user auth/account flows."""

    @staticmethod
    def normalize_email(email: str) -> str:
        return str(email).lower().strip()

    def generate_tokens_for_user(self, user: User) -> dict[str, str]:
        refresh = RefreshToken.for_user(user)
        return {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
        }

    def create_user(
        self,
        *,
        name: str,
        email: str,
        password: str,
        profile_picture: Any | None = None,
        profile_picture_id: str | None = None,
    ) -> User:
        normalized_email = self.normalize_email(email)
        if User.objects.filter(email=normalized_email).exists():
            raise ValueError("duplicate_email")

        user = User(name=name, email=normalized_email)
        user.set_password(password)

        if profile_picture_id:
            image = Image.objects.filter(attachment_key=profile_picture_id).first()
            if not image:
                raise ValueError("invalid_profile_picture")
            user.profile_picture = image
        elif profile_picture:
            image = Image.objects.create(
                file=profile_picture,
                description=f"Profile picture for {normalized_email}",
            )
            user.profile_picture = image

        user.save()
        return user

    def update_user(
        self,
        *,
        user: User,
        name: str | None = None,
        email: str | None = None,
        profile_picture: Any | None = None,
        profile_picture_id: str | None = None,
    ) -> User:
        if name is not None:
            user.name = name

        if email is not None:
            normalized_email = self.normalize_email(email)
            if normalized_email != user.email and User.objects.filter(
                email=normalized_email
            ).exists():
                raise ValueError("duplicate_email")
            user.email = normalized_email

        if profile_picture_id:
            image = Image.objects.filter(attachment_key=profile_picture_id).first()
            if not image:
                raise ValueError("invalid_profile_picture")
            user.profile_picture = image
        elif profile_picture:
            image = Image.objects.create(
                file=profile_picture,
                description=f"Profile picture for {user.email}",
            )
            user.profile_picture = image

        user.save()
        return user

    def authenticate(self, *, email: str, password: str) -> User:
        normalized_email = self.normalize_email(email)
        try:
            user = User.objects.get(email=normalized_email)
        except User.DoesNotExist as exc:
            raise exceptions.AuthenticationFailed("Credenciais inválidas") from exc

        if not user.check_password(password):
            raise exceptions.AuthenticationFailed("Credenciais inválidas")

        return user

    @staticmethod
    def me_payload(user: User) -> dict[str, str]:
        return {
            "id": str(user.id),
            "name": user.name,
            "type": user.type,
            "email": user.email,
            "is_superuser": user.is_superuser,
            "profile_picture_id": (
                str(user.profile_picture.attachment_key)
                if user.profile_picture
                else None
            ),
            "profile_picture": (
                user.profile_picture.url
                if user.profile_picture
                else user.profile_picture_url or None
            ),
        }
