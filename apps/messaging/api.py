import hashlib
import hmac
import json
import re
from datetime import timedelta
from string import Formatter

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.catalog.models import Product
from apps.core.api import audit
from apps.orders.models import Customer, Order

from . import client
from .models import (
    WhatsAppAccount,
    WhatsAppCampaign,
    WhatsAppContact,
    WhatsAppMedia,
    WhatsAppMessage,
    WhatsAppWebhook,
)
from .services import (
    DEFAULT_TEMPLATE,
    EVENTS,
    FIELDS,
    account_for,
    phone_number,
)


class ManageWhatsApp(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user.is_authenticated
            and request.user.workspace_id
            and request.user.has_dashboard_access
            and request.user.role in ["owner", "manager"]
        )


@api_view(["POST"])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
@transaction.atomic
def unsubscribe(request):
    token = serializers.CharField(max_length=500).run_validation(request.data.get("token"))
    try:
        contact_id = signing.loads(token, salt="whatsapp-unsubscribe", max_age=60 * 60 * 24 * 365)
    except signing.BadSignature:
        raise ValidationError(
            "This unsubscribe link is invalid or expired. Reply STOP to the store instead."
        ) from None
    contact = get_object_or_404(WhatsAppContact.objects.select_for_update(), pk=contact_id)
    contact.opted_out, contact.marketing, contact.transactional = True, False, False
    contact.consent_note, contact.consent_at = (
        "Customer used signed unsubscribe link.",
        timezone.now(),
    )
    contact.save()
    WhatsAppMessage.objects.filter(contact=contact, state="PENDING").update(
        state="CANCELLED", error="Customer unsubscribed."
    )
    return Response({"detail": "Unsubscribed."})


class AccountSerializer(serializers.ModelSerializer):
    gap_seconds = serializers.IntegerField(min_value=30, max_value=3600)
    daily_limit = serializers.IntegerField(min_value=1, max_value=500)
    quiet_start = serializers.IntegerField(min_value=0, max_value=23)
    quiet_end = serializers.IntegerField(min_value=0, max_value=23)
    events = serializers.ListField(
        child=serializers.ChoiceField(choices=EVENTS), max_length=len(EVENTS)
    )
    templates = serializers.DictField(child=serializers.CharField(max_length=1500), required=False)

    class Meta:
        model = WhatsAppAccount
        fields = [
            "session",
            "enabled",
            "marketing_enabled",
            "events",
            "templates",
            "gap_seconds",
            "daily_limit",
            "quiet_start",
            "quiet_end",
            "session_status",
            "last_error",
            "checked_at",
        ]
        read_only_fields = ["session", "session_status", "last_error", "checked_at"]

    def validate_templates(self, value):
        for event, template in value.items():
            if event not in EVENTS:
                raise serializers.ValidationError("Unknown notification event.")
            try:
                for _, field, spec, conversion in Formatter().parse(template):
                    if field is not None and (field not in FIELDS or spec or conversion):
                        raise ValueError()
            except ValueError:
                raise serializers.ValidationError(
                    "Use only supported simple template placeholders."
                ) from None
        return value

    def validate(self, data):
        if data.get("quiet_start", self.instance.quiet_start) == data.get(
            "quiet_end", self.instance.quiet_end
        ):
            raise serializers.ValidationError(
                "Quiet hours must have different start and end hours."
            )
        if data.get("enabled") and (
            not client.configured()
            or not settings.WAHA_WEBHOOK_URL
            or not settings.WAHA_WEBHOOK_SECRET
        ):
            raise serializers.ValidationError(
                "Configure WAHA and its signed STOP webhook before enabling sends."
            )
        return data


@api_view(["GET", "PATCH", "POST"])
@permission_classes([ManageWhatsApp])
def account(request):
    ws = request.user.workspace
    saved = WhatsAppAccount.objects.filter(workspace=ws).first()
    if request.method == "GET":
        from apps.logistics.models import TrackingWorkerState

        heartbeat = (
            TrackingWorkerState.objects.filter(name="whatsapp")
            .values_list("heartbeat", flat=True)
            .first()
        )
        return Response(
            {
                "configured": client.configured(),
                "worker_running": bool(
                    heartbeat and heartbeat > timezone.now() - timedelta(seconds=120)
                ),
                "webhook_configured": bool(
                    settings.WAHA_WEBHOOK_URL and settings.WAHA_WEBHOOK_SECRET
                ),
                "mode": settings.WAHA_SESSION_MODE,
                "workspace_id": str(ws.pk),
                "account": AccountSerializer(saved).data if saved else None,
                "events": EVENTS,
                "default_template": DEFAULT_TEMPLATE,
                "placeholders": sorted(FIELDS),
            }
        )
    saved = account_for(ws)
    if request.method == "PATCH":
        with transaction.atomic():
            saved = WhatsAppAccount.objects.select_for_update().get(pk=saved.pk)
            serializer = AccountSerializer(saved, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            audit(request, "whatsapp.preferences_updated", saved)
        return Response(serializer.data)
    operation = serializers.ChoiceField(choices=["connect", "status", "qr", "stop"]).run_validation(
        request.data.get("action")
    )
    try:
        if operation == "stop":
            WhatsAppAccount.objects.filter(pk=saved.pk).update(enabled=False)
            client.request("POST", f"/api/sessions/{saved.session}/stop")
            state = {"status": "STOPPED"}
        elif operation == "qr":
            data = client.request("GET", f"/api/{saved.session}/auth/qr?format=image")
            if (
                data.get("mimetype") != "image/png"
                or not isinstance(data.get("data"), str)
                or not re.fullmatch(r"[A-Za-z0-9+/=\r\n]+", data["data"])
            ):
                raise client.WahaError(
                    "WAHA did not return a supported QR image. Refresh session status."
                )
            response = Response({"qr": "data:image/png;base64," + data["data"]})
            response["Cache-Control"] = "no-store"
            return response
        elif operation == "connect":
            if (
                not settings.WAHA_WEBHOOK_URL.startswith("https://")
                or not settings.WAHA_WEBHOOK_SECRET
            ):
                raise ValidationError(
                    "Set HTTPS WAHA_WEBHOOK_URL and WAHA_WEBHOOK_SECRET before connecting."
                )
            hook = {
                "url": settings.WAHA_WEBHOOK_URL,
                "events": ["message", "session.status", "message.ack"],
                "hmac": {"key": settings.WAHA_WEBHOOK_SECRET},
            }
            try:
                state = client.request("GET", f"/api/sessions/{saved.session}")
            except client.WahaError as exc:
                if exc.status != 404:
                    raise
                state = client.request(
                    "POST",
                    "/api/sessions",
                    {"name": saved.session, "start": True, "config": {"webhooks": [hook]}},
                )
            else:
                config = state.get("config") or {}
                config["webhooks"] = [
                    h
                    for h in config.get("webhooks", [])
                    if h.get("url") != settings.WAHA_WEBHOOK_URL
                ] + [hook]
                client.request(
                    "PUT",
                    f"/api/sessions/{saved.session}",
                    {"name": saved.session, "config": config},
                )
                if state.get("status") in ["STOPPED", "FAILED"]:
                    client.request("POST", f"/api/sessions/{saved.session}/start")
            state = client.request("GET", f"/api/sessions/{saved.session}")
        else:
            state = client.request("GET", f"/api/sessions/{saved.session}")
        WhatsAppAccount.objects.filter(pk=saved.pk).update(
            session_status=str(state.get("status", "UNKNOWN"))[:40],
            last_error="",
            checked_at=timezone.now(),
        )
        audit(request, "whatsapp." + operation, saved)
        return Response({"status": state.get("status", "UNKNOWN")})
    except client.WahaError as exc:
        WhatsAppAccount.objects.filter(pk=saved.pk).update(last_error=str(exc)[:250])
        return Response({"detail": str(exc)}, status=502)


class ConsentInput(serializers.Serializer):
    customer_id = serializers.UUIDField()
    transactional = serializers.BooleanField()
    marketing = serializers.BooleanField()
    opted_out = serializers.BooleanField(default=False)
    consent_note = serializers.CharField(max_length=250, min_length=5)


@api_view(["GET", "POST"])
@permission_classes([ManageWhatsApp])
def contacts(request):
    ws = request.user.workspace
    if request.method == "GET":
        search = request.query_params.get("search", "")[:100]
        offset = serializers.IntegerField(min_value=0).run_validation(
            request.query_params.get("offset", 0)
        )
        return Response(
            list(
                WhatsAppContact.objects.filter(workspace=ws)
                .filter(Q(name__icontains=search) | Q(phone__icontains=search))
                .values(
                    "id",
                    "name",
                    "phone",
                    "transactional",
                    "marketing",
                    "opted_out",
                    "consent_note",
                    "consent_at",
                )[offset : offset + 100]
            )
        )
    data = ConsentInput(data=request.data)
    data.is_valid(raise_exception=True)
    values = data.validated_data
    customer = get_object_or_404(Customer, pk=values.pop("customer_id"), workspace=ws)
    phone = phone_number(customer.phone)
    if values["opted_out"]:
        values.update(transactional=False, marketing=False)
    with transaction.atomic():
        contact, _ = WhatsAppContact.objects.update_or_create(
            workspace=ws,
            phone=phone,
            defaults={**values, "name": customer.name, "consent_at": timezone.now()},
        )
        if not contact.transactional:
            WhatsAppMessage.objects.filter(contact=contact, state="PENDING").exclude(
                kind="MARKETING"
            ).update(state="CANCELLED", error="Consent withdrawn.")
        if not contact.marketing:
            WhatsAppMessage.objects.filter(
                contact=contact, state="PENDING", kind="MARKETING"
            ).update(state="CANCELLED", error="Consent withdrawn.")
        audit(request, "whatsapp.consent_updated", contact, values)
    return Response(
        {
            "detail": "Consent saved for this phone number. Existing opt-ins are never inferred from orders."
        }
    )


@api_view(["POST"])
@permission_classes([ManageWhatsApp])
@transaction.atomic
def remove_contact(request, pk):
    contact = get_object_or_404(
        WhatsAppContact.objects.select_for_update(), pk=pk, workspace=request.user.workspace
    )
    contact.opted_out = True
    contact.transactional = contact.marketing = False
    contact.save(update_fields=["opted_out", "transactional", "marketing", "updated_at"])
    WhatsAppMessage.objects.filter(contact=contact, state="PENDING").update(
        state="CANCELLED", error="Customer removed from WhatsApp by the team."
    )
    audit(request, "whatsapp.contact_removed", contact)
    return Response(
        {
            "detail": "Removed from WhatsApp. Customer and orders are unchanged; in-flight messages cannot be recalled."
        }
    )


class CampaignInput(serializers.Serializer):
    kind = serializers.ChoiceField(choices=["PRODUCT", "BROADCAST"], default="PRODUCT")
    audience_mode = serializers.ChoiceField(choices=["ALL", "SPECIFIC"], default="ALL")
    recipient_ids = serializers.ListField(
        child=serializers.UUIDField(), max_length=1000, default=list
    )
    media_id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=100)
    body = serializers.CharField(max_length=2000)
    product_ids = serializers.ListField(child=serializers.UUIDField(), max_length=5, default=list)
    scheduled_at = serializers.DateTimeField(default=timezone.now)


def campaign_recipients(campaign):
    rows = WhatsAppContact.objects.filter(
        workspace_id=campaign.workspace_id, marketing=True, opted_out=False
    )
    if campaign.audience_mode == "SPECIFIC":
        rows = rows.filter(pk__in=campaign.recipient_ids)
    return rows


def campaign_data(campaign):
    return {
        "id": str(campaign.pk),
        "name": campaign.name,
        "body": campaign.body,
        "products": campaign.product_snapshot,
        "kind": campaign.kind,
        "audience_mode": campaign.audience_mode,
        "recipient_ids": campaign.recipient_ids,
        "media": {
            "id": str(campaign.media_id),
            "url": campaign.media.url,
            "filename": campaign.media.filename,
            "mimetype": campaign.media.mimetype,
        }
        if campaign.media_id
        else None,
        "scheduled_at": campaign.scheduled_at,
        "state": campaign.state,
        "audience_count": campaign.audience_count,
        "eligible_count": campaign_recipients(campaign).count(),
        "counts": dict(
            campaign.whatsappmessage_set.values_list("state").annotate(total=Count("id"))
        ),
    }


@api_view(["GET", "POST"])
@permission_classes([ManageWhatsApp])
def campaigns(request):
    ws = request.user.workspace
    if request.method == "GET":
        return Response(
            [campaign_data(c) for c in WhatsAppCampaign.objects.filter(workspace=ws)[:50]]
        )
    serializer = CampaignInput(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    ids = set(data.pop("product_ids"))
    if data["kind"] == "PRODUCT" and not ids:
        raise ValidationError("Select at least one product.")
    if data["kind"] == "BROADCAST" and ids:
        raise ValidationError("Use a product campaign to attach products.")
    recipient_ids = set(data["recipient_ids"])
    if data["audience_mode"] == "SPECIFIC":
        if not recipient_ids or WhatsAppContact.objects.filter(
            workspace=ws, pk__in=recipient_ids, opted_out=False, marketing=True
        ).count() != len(recipient_ids):
            raise ValidationError("Select enabled WhatsApp customers from this workspace.")
    elif recipient_ids:
        raise ValidationError("All customers cannot also specify a recipient list.")
    media = None
    if data.get("media_id"):
        media = get_object_or_404(WhatsAppMedia, pk=data["media_id"], workspace=ws)
    products = list(Product.objects.filter(pk__in=ids, workspace=ws, is_active=True))
    if len(products) != len(ids):
        raise ValidationError("Choose active products from this workspace only.")
    if data["scheduled_at"] > timezone.now() + timedelta(days=30):
        raise ValidationError("Schedule within the next 30 days.")
    snapshot = [
        {"id": str(p.pk), "name": p.name, "price": str(p.selling_price), "image_url": p.image_url}
        for p in products
    ]
    text = (
        "*"
        + ws.name
        + "*"
        + "\n"
        + data["body"]
        + "\n\n"
        + "\n".join(f"*{p.name}* — PKR {p.selling_price}" for p in products)
    )
    if media and len(text) > 1024:
        raise ValidationError(
            "Media captions allow 1024 characters including store and product details. Shorten the announcement."
        )
    campaign = WhatsAppCampaign.objects.create(
        workspace=ws,
        name=data["name"],
        kind=data["kind"],
        audience_mode=data["audience_mode"],
        recipient_ids=[str(pk) for pk in recipient_ids],
        media=media,
        body=text,
        product_snapshot=snapshot,
        scheduled_at=data["scheduled_at"],
    )
    audit(request, "whatsapp.campaign_drafted", campaign)
    return Response(campaign_data(campaign), status=201)


@api_view(["POST"])
@permission_classes([ManageWhatsApp])
@transaction.atomic
def campaign_action(request, pk):
    campaign = get_object_or_404(
        WhatsAppCampaign.objects.select_for_update(), pk=pk, workspace=request.user.workspace
    )
    operation = serializers.ChoiceField(
        choices=["approve", "pause", "resume", "cancel"]
    ).run_validation(request.data.get("action"))
    if operation == "approve":
        if campaign.state != "DRAFT":
            raise ValidationError("Only a draft can be approved; it cannot be sent twice.")
        if request.data.get("confirm") is not True:
            raise ValidationError("Review the message and confirm sending to enabled customers.")
        account = account_for(request.user.workspace)
        if not account.enabled or not account.marketing_enabled or not client.configured():
            raise ValidationError("Enable WhatsApp and marketing in Settings first.")
        recipients = list(campaign_recipients(campaign)[:1001])
        if not recipients or len(recipients) > 1000:
            raise ValidationError("Campaign requires 1–1000 enabled recipients.")
        for contact in recipients:
            body = campaign.body
            if campaign.media_id and len(body) > 1024:
                raise ValidationError(
                    "Media caption exceeds 1024 characters. Create a shorter draft."
                )
            WhatsAppMessage.objects.create(
                workspace=request.user.workspace,
                account=account,
                contact=contact,
                campaign=campaign,
                kind="MARKETING",
                dedup_key=f"campaign:{campaign.pk}:{contact.phone}",
                body=body,
                due_at=max(timezone.now(), campaign.scheduled_at),
                expires_at=max(timezone.now(), campaign.scheduled_at) + timedelta(days=7),
            )
        campaign.audience_count = len(recipients)
        campaign.state = "RUNNING"
    elif operation == "pause" and campaign.state == "RUNNING":
        campaign.state = "PAUSED"
    elif operation == "resume" and campaign.state == "PAUSED":
        campaign.state = "RUNNING"
    elif operation == "cancel" and campaign.state in ["DRAFT", "RUNNING", "PAUSED"]:
        campaign.state = "CANCELLED"
        WhatsAppMessage.objects.filter(campaign=campaign, state="PENDING").update(
            state="CANCELLED", error="Campaign cancelled."
        )
    else:
        raise ValidationError("This campaign action is not available.")
    campaign.save()
    audit(request, "whatsapp.campaign_" + operation, campaign)
    return Response(campaign_data(campaign))


@api_view(["GET", "POST"])
@permission_classes([ManageWhatsApp])
def messages(request):
    ws = request.user.workspace
    if request.method == "GET":
        rows = WhatsAppMessage.objects.filter(workspace=ws)
        if request.query_params.get("order"):
            order_id = serializers.UUIDField().run_validation(request.query_params["order"])
            rows = rows.filter(order_id=order_id)
        return Response(
            list(
                rows.values(
                    "id",
                    "contact__name",
                    "contact__phone",
                    "kind",
                    "event",
                    "body",
                    "state",
                    "ack",
                    "provider_id",
                    "due_at",
                    "sent_at",
                    "error",
                )[:100]
            )
        )
    order_id = serializers.UUIDField().run_validation(request.data.get("order_id"))
    order = get_object_or_404(Order, pk=order_id, workspace=ws)
    body = serializers.CharField(max_length=2000).run_validation(request.data.get("body"))
    key = serializers.UUIDField().run_validation(request.data.get("request_id"))
    account = account_for(ws)
    if not account.enabled:
        raise ValidationError("Enable WhatsApp sends first; manual wa.me links remain available.")
    contact = WhatsAppContact.objects.filter(
        workspace=ws,
        phone=phone_number(order.customer_snapshot.get("phone", "")),
        transactional=True,
        opted_out=False,
    ).first()
    if not contact:
        raise ValidationError(
            "This order's phone is not enabled for WhatsApp. Check WhatsApp → Customers: it may be removed, unsubscribed, or different from the customer's current phone."
        )
    message, _ = WhatsAppMessage.objects.get_or_create(
        dedup_key=f"manual:{ws.pk}:{key}",
        defaults={
            "workspace": ws,
            "account": account,
            "contact": contact,
            "order": order,
            "kind": "MANUAL",
            "body": body,
            "due_at": timezone.now(),
            "expires_at": timezone.now() + timedelta(days=1),
        },
    )
    if message.order_id != order.pk:
        raise ValidationError("Request ID already used for another order.")
    audit(request, "whatsapp.manual_queued", message)
    return Response(
        {
            "detail": "Message queued. Sending limits and recipient exclusions apply.",
            "id": str(message.pk),
        },
        status=202,
    )


@api_view(["POST"])
@permission_classes([ManageWhatsApp])
def cancel_message(request, pk):
    message = get_object_or_404(WhatsAppMessage, pk=pk, workspace=request.user.workspace)
    if not WhatsAppMessage.objects.filter(pk=message.pk, state="PENDING").update(
        state="CANCELLED", error="Cancelled by team member."
    ):
        raise ValidationError(
            "Only pending messages can be cancelled. An in-flight send may already be accepted."
        )
    audit(request, "whatsapp.message_cancelled", message)
    return Response({"detail": "Queued message cancelled."})


@api_view(["POST"])
@permission_classes([ManageWhatsApp])
def check_message(request, pk):
    from .receipts import check_delivery

    message = get_object_or_404(
        WhatsAppMessage.objects.select_related("account", "contact"),
        pk=pk,
        workspace=request.user.workspace,
    )
    try:
        matched = check_delivery(message)
    except client.WahaError as exc:
        return Response({"detail": str(exc)}, status=502)
    return Response(
        {
            "detail": "Provider record matched; delivery status refreshed."
            if matched
            else "No unique match in recent WhatsApp history. Still unconfirmed; nothing was resent."
        }
    )


@api_view(["POST"])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
@transaction.atomic
def webhook(request):
    secret = settings.WAHA_WEBHOOK_SECRET
    raw = request.body
    if not secret or len(raw) > 262144:
        return Response(status=403)
    signature = hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest()
    if request.headers.get("X-Webhook-Hmac-Algorithm") != "sha512" or not hmac.compare_digest(
        signature, request.headers.get("X-Webhook-Hmac", "")
    ):
        return Response(status=403)
    try:
        data = json.loads(raw)
    except ValueError:
        return Response(status=400)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("payload"), dict)
        or not isinstance(data.get("session"), str)
    ):
        return Response(status=400)
    account = WhatsAppAccount.objects.filter(session=data["session"]).first()
    if not account:
        return Response(status=200)
    _, created = WhatsAppWebhook.objects.get_or_create(fingerprint=hashlib.sha256(raw).hexdigest())
    if not created:
        return Response(status=200)
    payload = data["payload"]
    if data.get("event") == "message.ack" and payload.get("fromMe") is True:
        from .receipts import acknowledgement, message_id, record_receipt

        record_receipt(account, message_id(payload), acknowledgement(payload))
        return Response(status=200)
    if (
        data.get("event") == "message"
        and payload.get("fromMe") is False
        and str(payload.get("body", "")).strip().upper()
        in {"STOP", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "BAND"}
    ):
        sender = str(payload.get("from", ""))
        if sender.endswith(("@c.us", "@s.whatsapp.net")):
            try:
                phone = phone_number(sender.split("@")[0])
            except ValidationError:
                return Response(status=200)
            contact, _ = WhatsAppContact.objects.update_or_create(
                workspace=account.workspace,
                phone=phone,
                defaults={
                    "opted_out": True,
                    "transactional": False,
                    "marketing": False,
                    "consent_note": "Customer sent STOP/unsubscribe.",
                    "consent_at": timezone.now(),
                },
            )
            WhatsAppMessage.objects.filter(contact=contact, state="PENDING").update(
                state="CANCELLED", error="Customer unsubscribed."
            )
    elif data.get("event") == "session.status":
        update = {
            "session_status": str(payload.get("status", "UNKNOWN"))[:40],
            "checked_at": timezone.now(),
        }
        restrictions = payload.get("data") or {}
        timelock = restrictions.get("reachoutTimelock") if isinstance(restrictions, dict) else None
        capping = restrictions.get("messageCapping") if isinstance(restrictions, dict) else None
        if (isinstance(timelock, dict) and timelock.get("isActive")) or (
            isinstance(capping, dict)
            and capping.get("cappingStatus") in ["FIRST_WARNING", "SECOND_WARNING", "CAPPED"]
        ):
            update.update(
                marketing_enabled=False,
                last_error="WhatsApp outreach restriction detected; marketing paused. Review the WAHA account before re-enabling.",
            )
        WhatsAppAccount.objects.filter(pk=account.pk).update(**update)
    return Response(status=200)
