from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('flood_camera_monitoring', '0024_curation_time_index'),
        ('users', '0001_initial'),
    ]
    operations = [
        migrations.AddField(model_name='cameraevidence', name='preserved_at', field=models.DateTimeField(null=True, blank=True)),
        migrations.AddField(model_name='cameraevidence', name='preserved_by', field=models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.PROTECT, related_name='preserved_camera_evidence', to='users.User')),
    ]
