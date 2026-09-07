import unicodedata

from django.db import migrations


SMALL_WORDS = {
    "a", "ao", "aos", "as", "da", "das", "de", "do", "dos", "e",
    "em", "na", "nas", "no", "nos",
}
CANONICAL_NAMES = {
    "nova brasilia": "Nova Brasília",
    "sao marcos": "São Marcos",
    "ulysses guimaraes": "Ulysses Guimarães",
}


def normalized(value):
    return " ".join(
        unicodedata.normalize("NFKD", value or "")
        .encode("ascii", "ignore")
        .decode()
        .casefold()
        .split()
    )


def canonical_name(value):
    formatted = []
    for index, word in enumerate((value or "").strip().split()):
        lower = word.casefold()
        if index > 0 and lower in SMALL_WORDS:
            formatted.append(lower)
        else:
            formatted.append(
                "-".join(part[:1].upper() + part[1:] for part in lower.split("-"))
            )
    result = " ".join(formatted)
    return CANONICAL_NAMES.get(normalized(result), result)


def normalize_names(apps, schema_editor):
    for model_name in ("City", "Region", "Neighborhood"):
        model = apps.get_model("addressing", model_name)
        objects = []
        for obj in model.objects.all().iterator():
            obj.name = canonical_name(obj.name)
            obj.normalized_name = normalized(obj.name)
            objects.append(obj)
        model.objects.bulk_update(objects, ["name", "normalized_name"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [("addressing", "0026_partition_reference_release_records")]
    operations = [
        migrations.RunPython(normalize_names, migrations.RunPython.noop),
    ]
