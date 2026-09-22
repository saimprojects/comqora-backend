from django.db import transaction

from .models import WhatsAppMessage, WhatsAppReceipt


def message_id(payload):
    """WAHA normalized IDs and WEBJS MessageId objects; never use event IDs."""
    value = payload.get("id") if isinstance(payload, dict) else None
    if isinstance(value, dict):
        value = value.get("_serialized") or value.get("serialized")
    return value if isinstance(value, str) and value.strip() and len(value) <= 250 else None


def acknowledgement(payload):
    if not isinstance(payload, dict):
        return None
    value = payload.get("ack")
    if type(value) is int and -1 <= value <= 4:
        return value
    name = payload.get("ackName")
    return (
        {"ERROR": -1, "PENDING": 0, "SERVER": 1, "DEVICE": 2, "READ": 3, "PLAYED": 4}.get(name)
        if isinstance(name, str)
        else None
    )


def check_delivery(message):
    """Read provider history only. Never resend an uncertain message."""
    from datetime import timedelta
    from urllib.parse import quote

    from django.utils import timezone

    from . import client

    if message.state not in {"SENT", "UNKNOWN"}:
        return False
    rows = client.request(
        "GET",
        f"/api/{quote(message.account.session, safe='')}/chats/{quote(message.contact.phone + '@c.us', safe='')}/messages?limit=100&downloadMedia=false",
    )
    if not isinstance(rows, list):
        return False
    matches = []
    for row in rows:
        if not isinstance(row, dict) or row.get("fromMe") is not True or not message_id(row):
            continue
        if message.provider_id:
            if message_id(row) == message.provider_id:
                matches.append(row)
        elif message.attempted_at and row.get("body") == message.body:
            stamp = row.get("timestamp")
            if type(stamp) in (int, float) and abs(stamp - message.attempted_at.timestamp()) <= 120:
                matches.append(row)
    if len(matches) != 1:
        return False
    match = matches[0]
    if not message.provider_id:
        # Identical nearby local attempts cannot be distinguished safely.
        if (
            WhatsAppMessage.objects.filter(
                account=message.account,
                contact=message.contact,
                body=message.body,
                attempted_at__range=(
                    message.attempted_at - timedelta(seconds=240),
                    message.attempted_at + timedelta(seconds=240),
                ),
            )
            .exclude(pk=message.pk)
            .exists()
        ):
            return False
        if (
            WhatsAppMessage.objects.filter(account=message.account, provider_id=message_id(match))
            .exclude(pk=message.pk)
            .exists()
        ):
            return False
        WhatsAppMessage.objects.filter(pk=message.pk, state="UNKNOWN", provider_id="").update(
            state="SENT",
            provider_id=message_id(match),
            sent_at=message.attempted_at or timezone.now(),
            error="",
        )
    record_receipt(message.account, message_id(match), acknowledgement(match))
    message.refresh_from_db()
    apply_saved_receipt(message)
    return True


@transaction.atomic
def record_receipt(account, provider_id, ack):
    if not provider_id or ack is None:
        return
    receipt, _ = WhatsAppReceipt.objects.get_or_create(account=account, provider_id=provider_id)
    WhatsAppReceipt.objects.filter(pk=receipt.pk, ack__lt=ack).update(ack=ack)
    receipt.refresh_from_db()
    WhatsAppMessage.objects.filter(
        account=account, provider_id=provider_id, ack__lt=receipt.ack
    ).update(ack=receipt.ack)


def apply_saved_receipt(message):
    receipt = WhatsAppReceipt.objects.filter(
        account_id=message.account_id, provider_id=message.provider_id
    ).first()
    if receipt:
        WhatsAppMessage.objects.filter(pk=message.pk, ack__lt=receipt.ack).update(ack=receipt.ack)
