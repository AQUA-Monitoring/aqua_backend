from django.db import migrations, models


def cast_type_to_integer(apps, schema_editor):
    """Converte a coluna textual herdada ``type`` em inteiro.

    A coluna anterior guardava rótulos livres; o único registro legado usa "1",
    que corresponde a ``Type.ALAGAMENTO``. Valores não numéricos viram 0 para
    que a coluna possa ficar NOT NULL, e ficam registrados no relatório do
    comando de importação.
    """
    table = schema_editor.quote_name("occurrences_occurrence")
    column = schema_editor.quote_name("type")
    schema_editor.execute(
        f"UPDATE {table} SET {column} = CASE"
        f" WHEN btrim({column}) ~ '^[0-9]+$' THEN btrim({column})"
        f" ELSE '0' END"
    )
    schema_editor.execute(
        f"ALTER TABLE {table} ALTER COLUMN {column} TYPE integer"
        f" USING {column}::integer"
    )


class Migration(migrations.Migration):
    dependencies = [("occurrences", "0005_remove_occurrence_neighborhood_choices")]

    operations = [
        migrations.RenameField(
            model_name="occurrence",
            old_name="date",
            new_name="datetime",
        ),
        migrations.AlterField(
            model_name="occurrence",
            name="datetime",
            field=models.DateTimeField(),
        ),
        migrations.RunPython(cast_type_to_integer, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="occurrence",
            name="type",
            field=models.IntegerField(
                choices=[
                    (1, "alagamento"),
                    (2, "inundacao"),
                    (3, "enxurrada"),
                    (4, "chuvas_intensas"),
                    (5, "vendaval"),
                    (6, "granizo"),
                    (7, "erosao de margem fluvial"),
                    (8, "colapso de edificacao"),
                    (9, "doencas infecciosas virais"),
                    (10, "transporte de produtos perigosos rodoviario"),
                ],
                default=1,
            ),
        ),
        migrations.AlterModelOptions(
            name="occurrence",
            options={
                "ordering": ("-datetime", "neighborhood", "pk"),
                "verbose_name_plural": "Occurrences",
            },
        ),
        migrations.AddField(
            model_name="occurrence",
            name="cause",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="damages",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="wind_speed_kmh",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="people_affected",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="deaths",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="injured",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="sick",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="sheltered",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="displaced",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="missing",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="time_precision",
            field=models.CharField(
                choices=[("minute", "minuto"), ("day", "dia")],
                default="day",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="source",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="source_ref",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="occurrence",
            name="source_row",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="occurrence",
            constraint=models.UniqueConstraint(
                fields=("source", "source_ref", "neighborhood"),
                name="unique_occurrence_source_row_neighborhood",
            ),
        ),
        migrations.AddIndex(
            model_name="occurrence",
            index=models.Index(
                fields=["neighborhood", "datetime"],
                name="occ_neigh_datetime_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="occurrence",
            index=models.Index(fields=["datetime"], name="occ_datetime_idx"),
        ),
    ]