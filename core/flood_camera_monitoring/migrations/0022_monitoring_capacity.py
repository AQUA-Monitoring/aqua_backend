from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('flood_camera_monitoring', '0021_model_review_and_shadow')]
    operations = [
        migrations.AddField('cameramonitoringstate', 'lease_queue', models.CharField(max_length=20, blank=True, default='')),
        migrations.AddField('cameramonitoringstate', 'lease_reserved_at', models.DateTimeField(null=True)),
        migrations.AddField('cameramonitoringstate', 'lease_started_at', models.DateTimeField(null=True)),
        migrations.AddField('cameramonitoringstate', 'capacity_deferred_at', models.DateTimeField(null=True)),
        migrations.AddField('cameramonitoringstate', 'policy_version', models.CharField(max_length=100, blank=True, default='')),
        migrations.AddIndex('cameraanalysisrun', models.Index(fields=['camera', '-started_at'], name='camera_run_latest_idx')),
    ]
