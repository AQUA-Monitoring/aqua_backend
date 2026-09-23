from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("flood_point_registering", "0009_floodpointneighborhood_and_more")]

    operations = [
        migrations.AddField(
            model_name="floodpointneighborhood",
            name="reference_base_revision",
            field=models.CharField(blank=True, max_length=160, null=True),
        ),
    ]
