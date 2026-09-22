from django.db.models.signals import post_save
from django.dispatch import receiver
from rest_framework.exceptions import ValidationError

from apps.orders.models import Customer

from .models import WhatsAppContact
from .services import phone_number


@receiver(post_save, sender=Customer)
def sync_customer(sender, instance, raw=False, using="default", **kwargs):
    if raw:
        return
    try:
        phone = phone_number(instance.phone)
    except ValidationError:
        return  # Invalid numbers cannot be WhatsApp recipients.
    # Never overwrite STOP/removal or re-enable a duplicate phone on customer edits.
    WhatsAppContact.objects.using(using).get_or_create(
        workspace_id=instance.workspace_id,
        phone=phone,
        defaults={"name": instance.name, "transactional": True, "marketing": True},
    )
