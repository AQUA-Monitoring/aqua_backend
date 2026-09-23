import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("addressing", "0026_partition_reference_release_records"),
        ("flood_impact", "0004_revision_affected_territory"),
        ("users", "0004_user_permission_flags"),
    ]

    operations = [
        migrations.AddField(
            model_name="floodspatialeventrevision",
            name="reference_base_release",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="flood_impact_revisions", to="addressing.referencebaserelease",
            ),
        ),
        migrations.AddField(
            model_name="roadfloodimpactrun",
            name="reason",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="roadfloodimpactrun",
            name="reference_base_release",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="flood_impact_runs", to="addressing.referencebaserelease",
            ),
        ),
        migrations.AddField(
            model_name="roadfloodimpactrun",
            name="requested_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="requested_road_impact_runs", to="users.user",
            ),
        ),
        migrations.AlterField(
            model_name="roadfloodimpactrun",
            name="status",
            field=models.CharField(
                choices=[
                    ("QUEUED", "Na fila"), ("RUNNING", "Executando"),
                    ("COMPLETED", "Concluído"), ("FAILED", "Falhou"),
                    ("STALE", "Desatualizado"),
                ],
                db_index=True, default="RUNNING", max_length=16,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="roadfloodimpactrun", name="uniq_road_impact_run",
        ),
        migrations.CreateModel(
            name="FloodImpactAdministrativeAction",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("action", models.CharField(
                    choices=[("PUBLISH", "Publicar"), ("REVOKE", "Revogar"), ("CONFIRM", "Confirmar"), ("RECALCULATE", "Recalcular")],
                    db_index=True, max_length=16,
                )),
                ("justification", models.TextField()),
                ("from_status", models.CharField(blank=True, max_length=16)),
                ("to_status", models.CharField(blank=True, max_length=16)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("actor", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="flood_impact_administrative_actions", to="users.user",
                )),
                ("derived_event", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="confirmation_actions", to="flood_impact.floodspatialevent",
                )),
                ("event", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="administrative_actions", to="flood_impact.floodspatialevent",
                )),
                ("impact_run", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="administrative_actions", to="flood_impact.roadfloodimpactrun",
                )),
                ("revision", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="administrative_actions", to="flood_impact.floodspatialeventrevision",
                )),
            ],
        ),
        migrations.AddIndex(
            model_name="floodimpactadministrativeaction",
            index=models.Index(fields=["event", "created_at"], name="flood_impac_event_action_idx"),
        ),
    ]
