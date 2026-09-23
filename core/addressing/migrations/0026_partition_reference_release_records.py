import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("addressing", "0025_referencebaserelease_referencebasereleaseaudit_and_more")]

    operations = [
        migrations.RemoveField(model_name="referencebaserelease", name="payload"),
        migrations.CreateModel(
            name="ReferenceBaseReleaseRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("partition", models.CharField(max_length=64)),
                ("ordinal", models.PositiveBigIntegerField()),
                ("record", models.JSONField()),
                ("release", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="records", to="addressing.referencebaserelease")),
            ],
        ),
        migrations.AddIndex(
            model_name="referencebasereleaserecord",
            index=models.Index(fields=["release", "partition", "ordinal"], name="addressing__release_42d725_idx"),
        ),
        migrations.AddConstraint(
            model_name="referencebasereleaserecord",
            constraint=models.UniqueConstraint(fields=("release", "partition", "ordinal"), name="uniq_release_partition_ordinal"),
        ),
    ]
