from django.db import models


class DemoVideoSource(models.Model):
    class Mode(models.TextChoices):
        AUTO = "auto", "Automático"
        NORMAL = "normal", "Normal"
        FLOODED = "flooded", "Alagado"

    class Status(models.TextChoices):
        PROCESSING = "processing", "Processando"
        READY = "ready", "Pronto"
        ERROR = "error", "Erro"

    mode = models.CharField(max_length=16, choices=Mode.choices, unique=True)
    video = models.ForeignKey(
        "uploader.Video",
        on_delete=models.PROTECT,
        related_name="demo_source_slots",
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ERROR
    )
    error = models.TextField(blank=True, default="Nenhum vídeo enviado.")
    active = models.BooleanField(default=False)
    updated_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        related_name="updated_demo_video_sources",
        null=True,
        blank=True,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["mode"]

    def __str__(self) -> str:
        return f"{self.mode}: {self.status}"
