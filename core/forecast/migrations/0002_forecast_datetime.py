from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("forecast", "0001_initial")]

    operations = [
        migrations.AlterUniqueTogether(name="forecast", unique_together=set()),
        migrations.RenameField(model_name="forecast", old_name="date", new_name="datetime"),
        migrations.AlterField(model_name="forecast", name="datetime", field=models.DateTimeField()),
        # Legacy dates denote local calendar days, not midnight UTC.
        migrations.RunSQL(
            "UPDATE forecast_forecast SET datetime = datetime AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo'",
            "UPDATE forecast_forecast SET datetime = datetime AT TIME ZONE 'America/Sao_Paulo' AT TIME ZONE 'UTC'",
        ),
        migrations.AlterUniqueTogether(name="forecast", unique_together={("latitude", "longitude", "datetime")}),
    ]
