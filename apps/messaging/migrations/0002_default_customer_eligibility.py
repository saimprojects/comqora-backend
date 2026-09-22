import re

from django.db import migrations, models


def populate(apps, schema_editor):
    Contact = apps.get_model("messaging", "WhatsAppContact")
    Customer = apps.get_model("orders", "Customer")
    db = schema_editor.connection.alias
    # One-time rollout approved by the platform owner. Never undo an opt-out.
    Contact.objects.using(db).filter(opted_out=False).update(transactional=True, marketing=True)
    for customer in Customer.objects.using(db).all().iterator():
        phone = re.sub(r"[\s()+-]", "", customer.phone)
        if phone.startswith("00"):
            phone = phone[2:]
        if re.fullmatch(r"03\d{9}", phone):
            phone = "92" + phone[1:]
        if not re.fullmatch(r"[1-9]\d{9,14}", phone):
            continue
        Contact.objects.using(db).get_or_create(
            workspace_id=customer.workspace_id,
            phone=phone,
            defaults={"name": customer.name, "transactional": True, "marketing": True},
        )


class Migration(migrations.Migration):
    dependencies = [("messaging", "0001_initial")]
    operations = [
        migrations.AlterField("whatsappcontact", "transactional", models.BooleanField(default=True)),
        migrations.AlterField("whatsappcontact", "marketing", models.BooleanField(default=True)),
        # No reverse: we cannot reconstruct prior eligibility or safely erase recipients.
        migrations.RunPython(populate),
    ]
