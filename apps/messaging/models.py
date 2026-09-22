from django.db import models

from apps.core.models import TenantModel


class WhatsAppAccount(TenantModel):
    workspace = models.OneToOneField("core.Workspace", on_delete=models.PROTECT)
    session = models.CharField(max_length=80, unique=True)
    enabled = models.BooleanField(default=False)
    marketing_enabled = models.BooleanField(default=False)
    events = models.JSONField(default=list)
    templates = models.JSONField(default=dict)
    gap_seconds = models.PositiveIntegerField(default=60)
    daily_limit = models.PositiveIntegerField(default=100)
    quiet_start = models.PositiveSmallIntegerField(default=20)
    quiet_end = models.PositiveSmallIntegerField(default=9)
    session_status = models.CharField(max_length=40, default="NOT_CONNECTED")
    last_error = models.CharField(max_length=250, blank=True)
    next_send_at = models.DateTimeField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)


class WhatsAppContact(TenantModel):
    phone = models.CharField(max_length=15)
    name = models.CharField(max_length=100)
    # Messaging eligibility, not evidence of customer consent.
    transactional = models.BooleanField(default=True)
    marketing = models.BooleanField(default=True)
    opted_out = models.BooleanField(default=False)
    consent_note = models.CharField(max_length=250, blank=True)
    consent_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["workspace", "phone"], name="unique_wa_contact")
        ]


class WhatsAppMedia(TenantModel):
    url = models.URLField(max_length=1000)
    filename = models.CharField(max_length=150)
    mimetype = models.CharField(max_length=80)
    size = models.PositiveIntegerField()


class WhatsAppCampaign(TenantModel):
    kind = models.CharField(max_length=16, default="PRODUCT")
    audience_mode = models.CharField(max_length=16, default="ALL")
    recipient_ids = models.JSONField(default=list)
    media = models.ForeignKey(WhatsAppMedia, null=True, blank=True, on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    body = models.TextField()
    product_snapshot = models.JSONField(default=list)
    scheduled_at = models.DateTimeField()
    state = models.CharField(max_length=16, default="DRAFT")
    audience_count = models.PositiveIntegerField(default=0)


class WhatsAppMessage(TenantModel):
    account = models.ForeignKey(WhatsAppAccount, on_delete=models.PROTECT)
    contact = models.ForeignKey(WhatsAppContact, on_delete=models.PROTECT)
    order = models.ForeignKey("orders.Order", null=True, blank=True, on_delete=models.PROTECT)
    campaign = models.ForeignKey(WhatsAppCampaign, null=True, blank=True, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16)
    event = models.CharField(max_length=40, blank=True)
    dedup_key = models.CharField(max_length=200, unique=True)
    body = models.TextField()
    state = models.CharField(max_length=16, default="PENDING", db_index=True)
    due_at = models.DateTimeField(db_index=True)
    expires_at = models.DateTimeField()
    attempted_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    provider_id = models.CharField(max_length=250, blank=True)
    ack = models.SmallIntegerField(default=-2)
    error = models.CharField(max_length=250, blank=True)


class WhatsAppWebhook(models.Model):
    fingerprint = models.CharField(max_length=64, primary_key=True)
    created_at = models.DateTimeField(auto_now_add=True)


class WhatsAppReceipt(models.Model):
    account = models.ForeignKey(WhatsAppAccount, on_delete=models.CASCADE)
    provider_id = models.CharField(max_length=250)
    ack = models.SmallIntegerField(default=-2)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "provider_id"], name="unique_wa_receipt")
        ]
