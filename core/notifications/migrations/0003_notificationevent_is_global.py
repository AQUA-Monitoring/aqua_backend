from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("notifications", "0002_unified_notification_domain")]

    operations = [
        migrations.AddField(
            model_name="notificationevent",
            name="is_global",
            field=models.BooleanField(default=False),
        ),
    ]
