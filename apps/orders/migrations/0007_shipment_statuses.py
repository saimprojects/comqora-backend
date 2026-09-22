from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0006_customer_province_order_charges_mode_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="status",
            field=models.CharField(
                choices=[
                    ("CREATED", "Created"),
                    ("IN_TRANSIT", "In Transit"),
                    ("OUT_FOR_DELIVERY", "Out For Delivery"),
                    ("DELIVERY_FAILED", "Delivery Failed"),
                    ("RETURN_IN_TRANSIT", "Return In Transit"),
                    ("DELIVERED", "Delivered"),
                    ("RETURNED", "Returned"),
                    ("CANCELLED", "Cancelled"),
                ],
                default="CREATED",
                max_length=24,
            ),
        ),
    ]
