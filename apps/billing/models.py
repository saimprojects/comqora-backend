import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


class Plan(models.Model):
    name = models.CharField(max_length=80)
    slug = models.SlugField(unique=True)
    monthly_price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("1"))]
    )
    ai_enabled = models.BooleanField(default=False)
    active = models.BooleanField(
        default=True,
        help_text="Available for new checkouts. Existing subscriptions keep their access.",
    )

    class Meta:
        ordering = ["monthly_price"]

    def __str__(self):
        return self.name


class PaymentBank(models.Model):
    bank_name = models.CharField(max_length=120)
    account_title = models.CharField(max_length=160)
    account_number = models.CharField(max_length=80)
    iban = models.CharField(max_length=50, blank=True)
    instructions = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.bank_name} — {self.account_title}"


class Subscription(models.Model):
    workspace = models.OneToOneField("core.Workspace", on_delete=models.PROTECT)
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT)
    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    suspended = models.BooleanField(default=False)

    @property
    def is_active(self):
        return not self.suspended and self.starts_at <= timezone.now() < self.expires_at

    def __str__(self):
        return f"{self.workspace} — {self.plan}"


class Payment(models.Model):
    STATES = [
        (s, s.replace("_", " ").title())
        for s in ["AWAITING_PROOF", "PENDING", "APPROVED", "REJECTED", "CANCELLED"]
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("core.Workspace", on_delete=models.PROTECT)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT)
    plan_name = models.CharField(max_length=80)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    bank = models.ForeignKey(PaymentBank, on_delete=models.PROTECT)
    bank_details = models.JSONField()
    status = models.CharField(max_length=20, choices=STATES, default="AWAITING_PROOF")
    reference = models.CharField(max_length=120, blank=True)
    proof = models.BinaryField(null=True, editable=False)
    proof_type = models.CharField(max_length=30, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reviewed_payments",
    )
    review_note = models.TextField(
        blank=True, help_text="Visible to the workspace owner. Add a reason before rejecting."
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace"],
                condition=models.Q(status__in=["AWAITING_PROOF", "PENDING"]),
                name="one_open_subscription_payment",
            )
        ]

    def __str__(self):
        return f"{self.workspace} — PKR {self.amount} — {self.status}"
