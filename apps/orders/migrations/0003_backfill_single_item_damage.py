from django.db import migrations
from django.db.models import Count


def backfill(apps, schema_editor):
    Order = apps.get_model('orders', 'Order')
    OrderItem = apps.get_model('orders', 'OrderItem')
    for order in Order.objects.filter(damaged_cost__gt=0).annotate(item_count=Count('items')).filter(item_count=1):
        OrderItem.objects.filter(order_id=order.pk).update(damaged_cost=order.damaged_cost)


class Migration(migrations.Migration):
    dependencies = [('orders', '0002_orderitem_damaged_cost')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
