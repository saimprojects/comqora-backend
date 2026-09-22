from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("billing", "0002_default_plans")]
    operations = [
        migrations.AddField(
            model_name="paymentbank",
            name="icon",
            field=models.BinaryField(blank=True, editable=False, null=True),
        ),
    ]
