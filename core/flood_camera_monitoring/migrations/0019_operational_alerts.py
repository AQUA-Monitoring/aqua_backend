import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("addressing", "0024_canonicalize_catalog_names"),
        ("users", "0003_user_profile_picture_fk_and_url"),
        ("flood_camera_monitoring", "0018_camera_territorial_context"),
    ]

    operations = [
        migrations.CreateModel(
            name="OperationalAlert",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("OPEN_INDICATION", "OPEN_INDICATION"), ("CONFIRMED", "CONFIRMED"), ("DISMISSED", "DISMISSED"), ("RESOLVED", "RESOLVED")], db_index=True, default="OPEN_INDICATION", max_length=24)),
                ("evidence", models.JSONField(default=dict)),
                ("first_detected_at", models.DateTimeField()),
                ("last_detected_at", models.DateTimeField()),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("dismissed_at", models.DateTimeField(blank=True, null=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("camera", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="operational_alerts", to="flood_camera_monitoring.camera")),
                ("confirmed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="confirmed_camera_alerts", to="users.user")),
                ("initial_detection", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="alerts_started", to="flood_camera_monitoring.flooddetectionrecord")),
                ("latest_detection", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="alerts_latest", to="flood_camera_monitoring.flooddetectionrecord")),
                ("region", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="camera_operational_alerts", to="addressing.region")),
            ],
            options={"ordering": ["-last_detected_at"]},
        ),
        migrations.CreateModel(
            name="OperationalAlertTransition",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("from_status", models.CharField(blank=True, choices=[("OPEN_INDICATION", "OPEN_INDICATION"), ("CONFIRMED", "CONFIRMED"), ("DISMISSED", "DISMISSED"), ("RESOLVED", "RESOLVED")], max_length=24, null=True)),
                ("to_status", models.CharField(choices=[("OPEN_INDICATION", "OPEN_INDICATION"), ("CONFIRMED", "CONFIRMED"), ("DISMISSED", "DISMISSED"), ("RESOLVED", "RESOLVED")], max_length=24)),
                ("origin", models.CharField(choices=[("CAMERA_ANALYSIS", "CAMERA_ANALYSIS"), ("ADMIN", "ADMIN")], max_length=24)),
                ("reason", models.TextField(blank=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="camera_alert_transitions", to="users.user")),
                ("alert", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="transitions", to="flood_camera_monitoring.operationalalert")),
            ],
            options={"ordering": ["created_at"]},
        ),
        migrations.CreateModel(
            name="AlertPublication",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=180)),
                ("message", models.TextField()),
                ("detected_at", models.DateTimeField()),
                ("confirmed_at", models.DateTimeField()),
                ("classification", models.CharField(max_length=32)),
                ("confidence", models.FloatField()),
                ("probabilities", models.JSONField(default=dict)),
                ("model_version", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("alert", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="publication", to="flood_camera_monitoring.operationalalert")),
                ("camera", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="alert_publications", to="flood_camera_monitoring.camera")),
                ("confirmed_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="alert_publications", to="users.user")),
                ("region", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="camera_alert_publications", to="addressing.region")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="operationalalert",
            constraint=models.UniqueConstraint(condition=models.Q(("status__in", ["OPEN_INDICATION", "CONFIRMED"])), fields=("camera",), name="uniq_active_operational_alert_per_camera"),
        ),
        migrations.AddIndex(model_name="operationalalert", index=models.Index(fields=["status", "last_detected_at"], name="flood_camer_status_975f5c_idx")),
        migrations.AddIndex(model_name="operationalalert", index=models.Index(fields=["region", "status"], name="flood_camer_region_9e7a91_idx")),
        migrations.AddIndex(model_name="operationalalerttransition", index=models.Index(fields=["alert", "created_at"], name="flood_camer_alert_i_b57fc9_idx")),
    ]
