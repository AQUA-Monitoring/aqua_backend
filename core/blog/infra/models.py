import uuid

from django.core.exceptions import ValidationError
from django.db import models

from core.uploader.models.image import Image


class Post(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200)
    subject = models.CharField(max_length=50)  # categoria (ex: Causas)
    author = models.CharField(max_length=100, null=True, blank=True)
    content = models.TextField(null=True, blank=True)
    banner_image = models.ForeignKey(Image, on_delete=models.SET_NULL, related_name="banner_images", null=True, blank=True)
    content_image = models.ForeignKey(Image, on_delete=models.SET_NULL, related_name="content_images", null=True, blank=True)
    reference_title = models.CharField(max_length=200, blank=True, default="")
    reference_url = models.URLField(max_length=1000, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(reference_title="", reference_url="")
                    | (~models.Q(reference_title="") & ~models.Q(reference_url=""))
                ),
                name="blog_post_reference_pair",
            ),
        ]

    def clean(self):
        super().clean()
        if bool(self.reference_title) != bool(self.reference_url):
            raise ValidationError(
                "O nome e a URL da referência devem ser informados juntos."
            )

    def __str__(self):
        return self.title
