from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('flood_camera_monitoring', '0025_evidence_preservation')]
    operations = [
        migrations.AddField(
            model_name='cameraevidence', name='extreme_preserved',
            field=models.BooleanField(default=False),
        ),
    ]
