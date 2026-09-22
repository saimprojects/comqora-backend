import re
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.orders.models import Order

from . import client
from .models import WhatsAppAccount, WhatsAppCampaign, WhatsAppContact, WhatsAppMessage

EVENTS = [*Order.STATUSES, "RETURN_RECEIVED", "REFUND_RECORDED"]
DEFAULT_TEMPLATE = "Hi {customer}, {store}: order {order_number} is {status}. Tracking: {tracking_id}. Courier: {courier}."
FIELDS = {"customer", "store", "order_number", "status", "tracking_id", "courier"}


def unsubscribe_footer(contact):
    token = signing.dumps(str(contact.pk), salt="whatsapp-unsubscribe")
    return (
        "\n\nReply STOP to unsubscribe, or open: "
        + settings.FRONTEND_URL.rstrip("/")
        + "/whatsapp-unsubscribe?token="
        + token
    )


def phone_number(value):
    value = re.sub(r"[\s()+-]", "", str(value))
    if value.startswith("00"):
        value = value[2:]
    if re.fullmatch(r"03\d{9}", value):
        value = "92" + value[1:]
    if not re.fullmatch(r"[1-9]\d{9,14}", value):
        raise ValidationError("Use a valid international phone number, or a Pakistan 03xx number.")
    return value


def account_for(workspace):
    with transaction.atomic():
        # Serialize Core allocation as well as same-workspace duplicate clicks.
        from apps.core.models import Workspace

        Workspace.objects.select_for_update().get(pk=workspace.pk)
        existing = WhatsAppAccount.objects.filter(workspace=workspace).first()
        if existing:
            return existing
        session = (
            "default" if settings.WAHA_SESSION_MODE == "CORE" else "sellflow-" + workspace.pk.hex
        )
        if (
            settings.WAHA_SESSION_MODE == "CORE"
            and str(workspace.pk) != settings.WAHA_CORE_WORKSPACE_ID
        ):
            raise ValidationError(
                "Set WAHA_CORE_WORKSPACE_ID to this workspace ID in the server environment before linking Core."
            )
        if WhatsAppAccount.objects.filter(session=session).exists():
            raise ValidationError(
                "Legacy single-session mode supports one linked workspace. Configure MULTI for isolated seller sessions."
            )
        from django.db import IntegrityError

        try:
            with transaction.atomic():
                return WhatsAppAccount.objects.create(workspace=workspace, session=session)
        except IntegrityError:
            raise ValidationError("This WAHA session is already assigned to a workspace.") from None


def enqueue_order(order, event, key):
    """Called inside the business transaction; only current transitions, never imported history."""
    account = WhatsAppAccount.objects.filter(workspace_id=order.workspace_id, enabled=True).first()
    if not account or event not in account.events:
        return
    try:
        phone = phone_number(order.customer_snapshot.get("phone", ""))
    except ValidationError:
        return
    contact = WhatsAppContact.objects.filter(
        workspace_id=order.workspace_id, phone=phone, transactional=True, opted_out=False
    ).first()
    if not contact:
        return
    values = {
        "customer": order.customer_snapshot.get("name", ""),
        "store": order.workspace.name,
        "order_number": order.number,
        "status": event.replace("_", " ").lower(),
        "tracking_id": order.tracking_id or "not assigned yet",
        "courier": order.courier_snapshot.get("courier", ""),
    }
    body = account.templates.get(event, DEFAULT_TEMPLATE).format(**values)
    now = timezone.now()
    WhatsAppMessage.objects.get_or_create(
        dedup_key=f"order:{order.pk}:{event}:{key}",
        defaults={
            "workspace_id": order.workspace_id,
            "account": account,
            "contact": contact,
            "order": order,
            "kind": "TRANSACTIONAL",
            "event": event,
            "body": body,
            "due_at": now,
            "expires_at": now + timedelta(days=1),
        },
    )


def quiet(account, now):
    hour = timezone.localtime(now).hour
    start, end = account.quiet_start, account.quiet_end
    return start <= hour < end if start < end else hour >= start or hour < end


def process_account(account_id):
    now = timezone.now()
    with transaction.atomic():
        account = WhatsAppAccount.objects.select_for_update().get(pk=account_id)
        # A crash after a send may mean WhatsApp accepted it; never blindly retry.
        WhatsAppMessage.objects.filter(
            account=account, state="SENDING", attempted_at__lt=now - timedelta(minutes=2)
        ).update(
            state="UNKNOWN",
            error="Worker interrupted during send. Check WhatsApp before any manual resend.",
        )
        if not account.enabled or (account.next_send_at and account.next_send_at > now):
            return 0
        if WhatsAppMessage.objects.filter(account=account, state="SENDING").exists():
            return 0
        count = WhatsAppMessage.objects.filter(
            account=account, attempted_at__gte=now - timedelta(hours=24)
        ).count()
        if count >= account.daily_limit:
            return 0
        pending = WhatsAppMessage.objects.filter(account=account, state="PENDING", due_at__lte=now)
        pending.filter(expires_at__lte=now).update(
            state="CANCELLED", error="Message expired before sending."
        )
        message = (
            pending.filter(expires_at__gt=now)
            .filter(Q(campaign__isnull=True) | Q(campaign__state="RUNNING"))
            .order_by(F("campaign_id").asc(nulls_first=True), "due_at", "created_at")
            .first()
        )
        if not message:
            return 0
        contact = WhatsAppContact.objects.select_for_update().get(pk=message.contact_id)
        permitted = not contact.opted_out and (
            contact.marketing and account.marketing_enabled
            if message.kind == "MARKETING"
            else contact.transactional
        )
        if message.kind == "TRANSACTIONAL" and message.event not in account.events:
            permitted = False
        if not permitted:
            message.state, message.error = (
                "CANCELLED",
                "Recipient or notification preference is disabled.",
            )
            message.save(update_fields=["state", "error"])
            return 0
        if message.kind == "MARKETING":
            if quiet(account, now):
                message.due_at = now + timedelta(minutes=30)
                message.save(update_fields=["due_at"])
                return 0
            if (
                WhatsAppMessage.objects.filter(
                    contact=contact, kind="MARKETING", attempted_at__gte=now - timedelta(days=7)
                )
                .exclude(pk=message.pk)
                .exists()
            ):
                message.state, message.error = (
                    "CANCELLED",
                    "Marketing frequency cap: one campaign per recipient per 7 days.",
                )
                message.save(update_fields=["state", "error"])
                return 0
        message.state, message.attempted_at = "SENDING", now
        # A concurrent cancel may have won after the candidate was selected.
        if not WhatsAppMessage.objects.filter(pk=message.pk, state="PENDING").update(
            state="SENDING", attempted_at=now
        ):
            return 0
        account.next_send_at = now + timedelta(seconds=max(30, account.gap_seconds))
        account.save(update_fields=["next_send_at"])
    sending = False
    try:
        session = client.request("GET", f"/api/sessions/{account.session}")
        if session.get("status") != "WORKING":
            # No send attempted, so this is safe to defer automatically.
            WhatsAppMessage.objects.filter(pk=message.pk, state="SENDING").update(
                state="PENDING",
                attempted_at=None,
                due_at=now + timedelta(minutes=5),
                error="Waiting for connected WhatsApp session.",
            )
            WhatsAppAccount.objects.filter(pk=account.pk).update(
                session_status=session.get("status", "UNKNOWN")[:40]
            )
            return 0
        # Recheck pause/consent after network preflight, immediately before sending.
        account.refresh_from_db()
        contact.refresh_from_db()
        campaign_running = (
            not message.campaign_id
            or WhatsAppCampaign.objects.filter(pk=message.campaign_id, state="RUNNING").exists()
        )
        if (
            not account.enabled
            or contact.opted_out
            or not campaign_running
            or (
                message.kind == "MARKETING"
                and (not contact.marketing or not account.marketing_enabled)
            )
            or (message.kind != "MARKETING" and not contact.transactional)
            or (message.kind == "TRANSACTIONAL" and message.event not in account.events)
        ):
            WhatsAppMessage.objects.filter(pk=message.pk).update(
                state="CANCELLED", error="Sending cancelled by updated preferences."
            )
            return 0
        sending = True
        payload = {"session": account.session, "chatId": contact.phone + "@c.us"}
        media = message.campaign.media if message.campaign_id else None
        endpoint = "/api/sendText"
        if media:
            endpoint = (
                "/api/sendImage"
                if media.mimetype.startswith("image/")
                else "/api/sendVideo"
                if media.mimetype == "video/mp4"
                else "/api/sendFile"
            )
            payload.update(
                file={"url": media.url, "filename": media.filename, "mimetype": media.mimetype},
                caption=message.body,
            )
        else:
            payload.update(text=message.body, linkPreview=False)
        result = client.request("POST", endpoint, payload)
        from .receipts import acknowledgement, apply_saved_receipt, message_id, record_receipt

        provider_id = message_id(result)
        if not isinstance(provider_id, str) or not provider_id:
            raise client.WahaError(
                "WAHA did not return a message ID. Check WhatsApp before resending.", uncertain=True
            )
        WhatsAppMessage.objects.filter(pk=message.pk).update(
            state="SENT", sent_at=timezone.now(), provider_id=provider_id[:250], error=""
        )
        record_receipt(account, provider_id, acknowledgement(result))
        message.provider_id = provider_id
        apply_saved_receipt(message)
        WhatsAppAccount.objects.filter(pk=account.pk).update(
            session_status="WORKING", last_error="", checked_at=timezone.now()
        )
        return 1
    except client.WahaError as exc:
        if not sending:
            WhatsAppMessage.objects.filter(pk=message.pk).update(
                state="PENDING",
                attempted_at=None,
                due_at=timezone.now() + timedelta(minutes=5),
                error=str(exc)[:250],
            )
            return 0
        WhatsAppMessage.objects.filter(pk=message.pk).update(
            state="UNKNOWN" if exc.uncertain else "FAILED", error=str(exc)[:250]
        )
        WhatsAppAccount.objects.filter(pk=account.pk).update(
            last_error=str(exc)[:250], next_send_at=timezone.now() + timedelta(minutes=5)
        )
        return 0


def process_outbox(limit=100):
    from apps.logistics.models import TrackingWorkerState

    TrackingWorkerState.objects.update_or_create(
        name="whatsapp", defaults={"heartbeat": timezone.now()}
    )
    if not client.configured():
        return 0
    count = 0
    for pk in (
        WhatsAppAccount.objects.filter(enabled=True)
        .order_by("next_send_at")
        .values_list("pk", flat=True)[:limit]
    ):
        TrackingWorkerState.objects.update_or_create(
            name="whatsapp", defaults={"heartbeat": timezone.now()}
        )
        count += process_account(pk)
    for campaign in WhatsAppCampaign.objects.filter(state="RUNNING"):
        if not WhatsAppMessage.objects.filter(
            campaign=campaign, state__in=["PENDING", "SENDING"]
        ).exists():
            campaign.state = "DONE"
            campaign.save(update_fields=["state"])
    return count
