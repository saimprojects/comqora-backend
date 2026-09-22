import re

from django.db import migrations


def backfill(apps, schema_editor):
    Courier = apps.get_model("logistics", "Courier")
    Order = apps.get_model("orders", "Order")
    aliases = {
        "tcs": "TCS",
        "leopards": "Leopards",
        "leoaprds": "Leopards",
        "mp": "M&P",
        "mnp": "M&P",
        "trax": "Trax",
        "daewoo": "Daewoo",
        "deawoo": "Daewoo",
        "dastaqlogistic": "Dastaq Logistic",
        "dastaqlogistics": "Dastaq Logistic",
        "ahl": "AHL",
        "postex": "PostEx",
    }
    for courier in Courier.objects.all().iterator():
        provider = (
            aliases.get(re.sub(r"[^a-z0-9]", "", courier.name.lower()))
            or aliases.get(re.sub(r"[^a-z0-9]", "", courier.code.lower()))
            or "Others"
        )
        Courier.objects.filter(pk=courier.pk).update(provider=provider)
        Order.objects.filter(courier_id=courier.pk, tracking_provider="").update(
            tracking_provider=provider
        )


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0004_order_tracking_attempted_at_and_more"),
        ("logistics", "0002_trackingworkerstate_courier_provider"),
    ]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
