from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

from apps.core.models import TenantModel


class Expense(TenantModel):
    name = models.CharField(max_length=150)
    category = models.CharField(
        max_length=40,
        choices=[(s, s.title()) for s in ["rent", "salary", "software", "utilities", "other"]],
        default="other",
    )
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    date = models.DateField()
    notes = models.TextField(blank=True)


class BankAccount(TenantModel):
    name = models.CharField(max_length=100)
    kind = models.CharField(
        max_length=10,
        choices=[("BANK", "Bank"), ("WALLET", "Wallet"), ("CASH", "Cash")],
        default="BANK",
    )
    last_four = models.CharField(max_length=4, blank=True)
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    opening_date = models.DateField()


class SettlementImport(TenantModel):
    courier = models.ForeignKey("logistics.Courier", on_delete=models.PROTECT)
    filename = models.CharField(max_length=200)
    digest = models.CharField(max_length=64)
    source = models.BinaryField(editable=False)
    status = models.CharField(
        max_length=12,
        default="QUEUED",
        choices=[
            (s, s.title()) for s in ["QUEUED", "PROCESSING", "REVIEW", "CONFIRMED", "VOID", "ERROR"]
        ],
    )
    extracted = models.JSONField(default=dict)
    review = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    error = models.CharField(max_length=250, blank=True)
    processing_token = models.UUIDField(null=True)
    processing_at = models.DateTimeField(null=True)
    reference = models.CharField(max_length=120, blank=True)
    reference_key = models.CharField(max_length=120, blank=True)
    date = models.DateField(null=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    confirmed_by = models.ForeignKey("accounts.User", null=True, on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(null=True)
    confirmed_result = models.JSONField(default=dict)
    replaces = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="revisions"
    )

    class Meta(TenantModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "digest"],
                condition=~models.Q(status="VOID"),
                name="unique_workspace_cpr_file",
            ),
            models.UniqueConstraint(
                fields=["workspace", "courier", "reference_key"],
                condition=models.Q(status="CONFIRMED"),
                name="unique_active_cpr_reference",
            ),
        ]


class SettlementCost(TenantModel):
    statement = models.ForeignKey(
        SettlementImport, related_name="cost_updates", on_delete=models.PROTECT
    )
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    basis = models.CharField(max_length=12)
    previous_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    previous_basis = models.CharField(max_length=12, blank=True)
    previous_source = models.UUIDField(null=True)

    class Meta(TenantModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["statement", "order"], name="unique_statement_order_cost"
            )
        ]


class SettlementMapping(TenantModel):
    courier = models.ForeignKey("logistics.Courier", on_delete=models.PROTECT)
    signature = models.CharField(max_length=64)
    roles = models.JSONField(default=list)
    statement = models.ForeignKey(SettlementImport, on_delete=models.PROTECT)

    class Meta(TenantModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "courier", "signature"],
                name="unique_settlement_layout_mapping",
            )
        ]


class BankEntry(TenantModel):
    expense = models.ForeignKey(
        Expense, null=True, blank=True, related_name="payments", on_delete=models.PROTECT
    )
    """Append-only cash register. Reversals are separate signed entries."""

    account = models.ForeignKey(BankAccount, related_name="entries", on_delete=models.PROTECT)
    statement = models.ForeignKey(
        SettlementImport, null=True, related_name="receipts", on_delete=models.PROTECT
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateField()
    reference = models.CharField(max_length=150)
    notes = models.CharField(max_length=500, blank=True)
    request_key = models.UUIDField()
    reversal_of = models.OneToOneField(
        "self", null=True, related_name="reversal", on_delete=models.PROTECT
    )
    actor = models.ForeignKey("accounts.User", on_delete=models.PROTECT)

    class Meta(TenantModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["workspace", "request_key"], name="unique_bank_request")
        ]
