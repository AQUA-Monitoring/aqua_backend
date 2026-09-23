import os

from django.db import migrations, models
import django.db.models.deletion


def create_demo_slots(apps, schema_editor):
    DemoVideoSource = apps.get_model("uploader", "DemoVideoSource")
    Video = apps.get_model("uploader", "Video")
    env_by_mode = {
        "auto": "DEMO_DYNAMIC_VIDEO_ATTACHMENT_KEY",
        "normal": "DEMO_NORMAL_VIDEO_ATTACHMENT_KEY",
        "flooded": "DEMO_FLOODED_VIDEO_ATTACHMENT_KEY",
    }
    for mode, env_name in env_by_mode.items():
        defaults = {}
        attachment_key = os.getenv(env_name, "").strip()
        if attachment_key:
            video = Video.objects.filter(attachment_key=attachment_key).first()
            if video is not None:
                defaults = {"video": video, "status": "ready", "error": ""}
        DemoVideoSource.objects.get_or_create(mode=mode, defaults=defaults)


class Migration(migrations.Migration):
    dependencies = [
        ("uploader", "0002_video_alter_image_file"),
        ("users", "0004_user_permission_flags"),
    ]

    operations = [
        migrations.CreateModel(
            name="DemoVideoSource",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("mode", models.CharField(choices=[("auto", "Automático"), ("normal", "Normal"), ("flooded", "Alagado")], max_length=16, unique=True)),
                ("status", models.CharField(choices=[("processing", "Processando"), ("ready", "Pronto"), ("error", "Erro")], default="error", max_length=16)),
                ("error", models.TextField(blank=True, default="Nenhum vídeo enviado.")),
                ("active", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="updated_demo_video_sources", to="users.user")),
                ("video", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="demo_source_slots", to="uploader.video")),
            ],
            options={"ordering": ["mode"]},
        ),
        migrations.RunPython(create_demo_slots, migrations.RunPython.noop),
    ]
