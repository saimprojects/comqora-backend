from django.db import migrations


def seed(apps, schema_editor):
    Plan = apps.get_model('billing', 'Plan')
    for slug, name, price, ai in [('ultra', 'Ultra', '2425.00', False), ('ultra-ai', 'Ultra AI', '4599.00', True)]:
        Plan.objects.get_or_create(slug=slug, defaults={'name': name, 'monthly_price': price, 'ai_enabled': ai})


class Migration(migrations.Migration):
    dependencies = [('billing', '0001_initial')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
