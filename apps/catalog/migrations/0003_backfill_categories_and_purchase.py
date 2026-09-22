from django.db import migrations
from django.db.models import F


def forwards(apps, schema_editor):
    Product = apps.get_model('catalog', 'Product')
    Category = apps.get_model('catalog', 'Category')
    for product in Product.objects.all().iterator():
        name = product.category.strip() or 'General'
        category = Category.objects.filter(workspace_id=product.workspace_id, name__iexact=name).first()
        if not category:
            category = Category.objects.create(workspace_id=product.workspace_id, name=name)
        Product.objects.filter(pk=product.pk).update(category_record=category, category=category.name)
    # Older receipts only retained landed cost, not the separate purchase components.
    apps.get_model('catalog', 'StockBatch').objects.update(purchase_amount=F('unit_cost'))


class Migration(migrations.Migration):
    dependencies = [('catalog', '0002_stockbatch_extra_costs_stockbatch_import_cost_and_more')]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
