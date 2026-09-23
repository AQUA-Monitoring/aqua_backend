import mimetypes
import uuid
from pathlib import Path

from django.db import models

from core.uploader.helpers.files import SVG_XML_CONTENT_TYPES, get_content_type


def image_file_path(image, filename: str) -> str:
    content_type = get_content_type(image.file)
    source_suffix = Path(filename or getattr(image.file, "name", "")).suffix.lower()
    extension: str | None
    if content_type == "image/svg+xml" or (
        content_type in SVG_XML_CONTENT_TYPES and source_suffix == ".svg"
    ):
        extension = ".svg"
    elif source_suffix and content_type in {"text/plain", "application/octet-stream"}:
        # Remote sync may receive a generic MIME from libmagic; retain the
        # source extension so attachment URLs remain stable and meaningful.
        extension = source_suffix
    else:
        extension = mimetypes.guess_extension(content_type or "")
    if not extension:
        extension = source_suffix
    if extension == ".jpe":
        extension = ".jpg"
    return f"images/{image.public_id}{extension or ''}"


class Image(models.Model):
    attachment_key = models.UUIDField(
        max_length=255,
        default=uuid.uuid4,
        unique=True,
        help_text=("Used to attach the image to another object. " "Cannot be used to retrieve the image file."),
    )
    public_id = models.UUIDField(
        max_length=255,
        default=uuid.uuid4,
        unique=True,
        help_text=(
            "Used to retrieve the image itself. "
            "Should not be readable until the image is attached to another object."
        ),
    )
    # FileField is intentional: Pillow-backed ImageField rejects valid SVGs.
    # The upload serializer performs content-aware validation for raster and SVG.
    file = models.FileField(upload_to=image_file_path)
    description = models.CharField(max_length=255, blank=True)
    uploaded_on = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.description} - {self.attachment_key}"

    @property
    def url(self) -> str | None:
        return self.file.url if self.file else None  # pylint: disable=no-member
