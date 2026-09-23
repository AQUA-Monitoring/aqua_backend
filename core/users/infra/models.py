from django.db import models
from django.contrib.auth.hashers import make_password, check_password
from core.uploader.models.image import Image
import uuid


class User(models.Model):
    class UserType(models.TextChoices):
        ADMIN = "admin", "ADMIN"
        STANDARD = "standard", "STANDARD"

    id: uuid.UUID = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False
    )
    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True, max_length=300, db_index=True)
    password = models.CharField(max_length=255, blank=True, default="")
    date_of_birth = models.DateField(null=True, blank=True)
    google_sub = models.CharField(
        max_length=255, unique=True, null=True, blank=True, db_index=True
    )
    profile_picture_url = models.URLField(blank=True, max_length=512)
    profile_picture = models.ForeignKey(
        Image,
        on_delete=models.SET_NULL,
        related_name="profile_pictures",
        null=True,
        blank=True,
    )
    type = models.CharField(
        max_length=20, choices=UserType.choices, default=UserType.STANDARD
    )
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.name} <{self.email}> ({self.get_type_display()})"

    # Basic password helpers to keep infra self-contained
    def set_password(self, raw_password: str) -> None:
        self.password = make_password(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password(raw_password, self.password)

    @property
    def is_authenticated(self) -> bool:  # type: ignore[override]
        return True

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["email"]),
            models.Index(fields=["google_sub"]),
        ]
