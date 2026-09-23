from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("blog", "0004_post_banner_image_post_content_image"),
    ]

    operations = [
        migrations.AddField(
            model_name="post",
            name="reference_title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="post",
            name="reference_url",
            field=models.URLField(blank=True, default="", max_length=1000),
        ),
        migrations.AddConstraint(
            model_name="post",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(reference_title="", reference_url="")
                    | (~models.Q(reference_title="") & ~models.Q(reference_url=""))
                ),
                name="blog_post_reference_pair",
            ),
        ),
    ]
