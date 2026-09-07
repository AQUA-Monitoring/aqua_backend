import django.contrib.gis.db.models.fields
import django.core.validators
import django.db.models.deletion
import uuid
from django.db import migrations, models


def backfill_camera_events(apps, schema_editor):
    Event = apps.get_model("notifications", "NotificationEvent")
    Delivery = apps.get_model("notifications", "PushDelivery")
    Publication = apps.get_model("flood_camera_monitoring", "AlertPublication")
    for publication in Publication.objects.select_related("alert").iterator():
        alert = publication.alert
        status = "RESOLVED" if alert.status == "RESOLVED" else "PUBLISHED"
        event, _ = Event.objects.get_or_create(
            idempotency_key=f"camera:{alert.pk}:confirmed",
            defaults={
                "origin": "CAMERA", "status": status, "severity": "CRITICAL",
                "title": publication.title, "message": publication.message,
                "destination_url": f"/cameras/{alert.camera_id}",
                "actor_id": publication.confirmed_by_id,
                "operational_alert_id": alert.pk,
                "published_at": publication.confirmed_at,
                "resolved_at": alert.resolved_at,
                "territory_snapshot": {},
            },
        )
        if publication.region_id:
            event.regions.add(publication.region_id)
        Delivery.objects.filter(operational_alert_id=alert.pk, event__isnull=True).update(event=event)


class Migration(migrations.Migration):
    dependencies = [
        ("addressing", "0027_normalize_territory_names"),
        ("flood_camera_monitoring", "0019_operational_alerts"),
        ("flood_point_registering", "0010_neighborhood_reference_revision"),
        ("notifications", "0001_initial"),
        ("users", "0004_user_permission_flags"),
    ]

    operations = [
        migrations.AlterModelOptions(name="regionsubscription", options={"ordering": ["created_at"]}),
        migrations.RemoveConstraint(model_name="regionsubscription", name="uniq_notification_user_region"),
        migrations.AlterField(model_name="regionsubscription", name="region", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="subscriptions", to="addressing.region")),
        migrations.AddField(model_name="regionsubscription", name="neighborhood", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="notification_subscriptions", to="addressing.neighborhood")),
        migrations.CreateModel(
            name="SavedPlace",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=80)),
                ("location", django.contrib.gis.db.models.fields.PointField(srid=4326)),
                ("radius_km", models.FloatField(default=3.0, validators=[django.core.validators.MinValueValidator(1.0), django.core.validators.MaxValueValidator(10.0)])),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("city", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to="addressing.city")),
                ("neighborhood", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to="addressing.neighborhood")),
                ("region", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to="addressing.region")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="saved_places", to="users.user")),
            ], options={"ordering": ["name", "created_at"]},
        ),
        migrations.CreateModel(
            name="NotificationEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("origin", models.CharField(choices=[("FLOOD_POINT", "Ponto de alagamento"), ("CAMERA", "Câmera"), ("MANUAL", "Manual")], db_index=True, max_length=20)),
                ("status", models.CharField(choices=[("DRAFT", "Rascunho"), ("PUBLISHED", "Publicado"), ("RESOLVED", "Encerrado"), ("CANCELED", "Cancelado")], db_index=True, default="DRAFT", max_length=16)),
                ("severity", models.CharField(choices=[("INFO", "Informativo"), ("ATTENTION", "Atenção"), ("CRITICAL", "Crítico")], default="ATTENTION", max_length=16)),
                ("title", models.CharField(max_length=160)),
                ("message", models.TextField(max_length=1000)),
                ("destination_url", models.CharField(blank=True, max_length=500)),
                ("idempotency_key", models.CharField(max_length=180, unique=True)),
                ("geometry", django.contrib.gis.db.models.fields.GeometryField(blank=True, null=True, srid=4326)),
                ("territory_snapshot", models.JSONField(blank=True, default=dict)),
                ("audience_count", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="notification_events", to="users.user")),
                ("flood_point", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="notification_events", to="flood_point_registering.flood_point_register")),
                ("neighborhoods", models.ManyToManyField(blank=True, related_name="notification_events", to="addressing.neighborhood")),
                ("operational_alert", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="notification_events", to="flood_camera_monitoring.operationalalert")),
                ("regions", models.ManyToManyField(blank=True, related_name="notification_events", to="addressing.region")),
            ], options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="NotificationEventAudit",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("action", models.CharField(max_length=32)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="users.user")),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="audit_log", to="notifications.notificationevent")),
            ],
        ),
        migrations.AddField(model_name="pushdelivery", name="event", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="push_deliveries", to="notifications.notificationevent")),
        migrations.AlterField(model_name="pushdelivery", name="operational_alert", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="push_deliveries", to="flood_camera_monitoring.operationalalert")),
        migrations.RunPython(backfill_camera_events, migrations.RunPython.noop),
        migrations.AddConstraint(model_name="regionsubscription", constraint=models.UniqueConstraint(condition=models.Q(("region__isnull", False)), fields=("user", "region"), name="uniq_notification_user_region")),
        migrations.AddConstraint(model_name="regionsubscription", constraint=models.UniqueConstraint(condition=models.Q(("neighborhood__isnull", False)), fields=("user", "neighborhood"), name="uniq_notification_user_neighborhood")),
        migrations.AddConstraint(model_name="regionsubscription", constraint=models.CheckConstraint(condition=models.Q(models.Q(("neighborhood__isnull", True), ("region__isnull", False)), models.Q(("neighborhood__isnull", False), ("region__isnull", True)), _connector="OR"), name="notification_subscription_one_territory")),
        migrations.AddIndex(model_name="notificationevent", index=models.Index(fields=["status", "created_at"], name="notificatio_status_b9ac86_idx")),
        migrations.AddConstraint(model_name="pushdelivery", constraint=models.UniqueConstraint(condition=models.Q(("event__isnull", False)), fields=("event", "subscription", "kind"), name="uniq_push_event_subscription_kind")),
    ]
