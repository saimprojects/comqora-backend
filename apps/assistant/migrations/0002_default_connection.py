from django.db import migrations


def seed(apps, schema_editor):
    apps.get_model("assistant", "Connection").objects.get_or_create(
        name="Fazita", key_environment_variable="FAZITA_API_KEY"
    )


class Migration(migrations.Migration):
    dependencies = [("assistant", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
