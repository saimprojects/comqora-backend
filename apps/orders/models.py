from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

from apps.core.models import TenantModel


class Customer(TenantModel):
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=25)
    email = models.EmailField(blank=True)
    city = models.CharField(max_length=80)
    province = models.CharField(max_length=80, blank=True)
    address = models.TextField()
    notes = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Order(TenantModel):
    ACTIVE_SHIPMENT_STATUSES = (
        "IN_TRANSIT",
        "OUT_FOR_DELIVERY",
        "DELIVERY_FAILED",
        "RETURN_IN_TRANSIT",
    )
    TRACKING_STATUSES = (*ACTIVE_SHIPMENT_STATUSES, "DELIVERED", "RETURNED")
    STATUSES = ["CREATED", *TRACKING_STATUSES, "CANCELLED"]
    number = models.CharField(max_length=24)
    customer = models.ForeignKey(Customer, related_name="orders", on_delete=models.PROTECT)
    customer_snapshot = models.JSONField(default=dict)
    courier = models.ForeignKey(
        "logistics.Courier", related_name="orders", on_delete=models.PROTECT
    )
    status = models.CharField(
        max_length=24,
        choices=[(s, s.replace("_", " ").title()) for s in STATUSES],
        default="CREATED",
    )
    tracking_id = models.CharField(max_length=100, blank=True)
    tracking_mode = models.CharField(
        max_length=8, choices=[("AUTO", "Automatic"), ("MANUAL", "Manual")], default="AUTO"
    )
    delivery_zone = models.CharField(
        max_length=24,
        choices=[
            ("SAME_CITY", "Same city"),
            ("SAME_PROVINCE", "Same province"),
            ("OUTSIDE_PROVINCE", "Outside province"),
        ],
        default="OUTSIDE_PROVINCE",
    )
    charges_mode = models.CharField(
        max_length=8,
        choices=[("ABSORB", "Deduct from sale"), ("ADD", "Add to sale")],
        default="ABSORB",
    )
    customer_charges = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tracking_provider = models.CharField(max_length=24, blank=True)
    tracking_checked_at = models.DateTimeField(null=True, blank=True)
    tracking_attempted_at = models.DateTimeField(null=True, blank=True)
    tracking_next_sync_at = models.DateTimeField(null=True, blank=True, db_index=True)
    tracking_error = models.CharField(max_length=250, blank=True)
    tracking_failures = models.PositiveIntegerField(default=0)
    tracking_lock_until = models.DateTimeField(null=True, blank=True)
    tracking_lock_token = models.UUIDField(null=True, blank=True)
    tracking_status_at = models.DateTimeField(null=True, blank=True)
    payment_type = models.CharField(
        max_length=20,
        choices=[("COD", "COD"), ("PREPAID", "Prepaid"), ("PARTIAL", "Partial advance")],
        default="COD",
    )
    advance_paid = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    refunded_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    discount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    product_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    ad_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    courier_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    actual_courier_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    actual_courier_cost_basis = models.CharField(max_length=12, blank=True)
    actual_courier_cost_source = models.UUIDField(null=True, blank=True)
    return_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    packaging_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    other_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    other_costs = models.JSONField(default=list)
    courier_snapshot = models.JSONField(default=dict)
    packaging_snapshot = models.JSONField(default=list)
    weight = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.5"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    notes = models.TextField(blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    return_received_at = models.DateTimeField(null=True, blank=True)
    damaged_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta(TenantModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["workspace", "number"], name="unique_workspace_order")
        ]
        indexes = [models.Index(fields=["workspace", "status", "created_at"])]

    def __str__(self):
        return self.number


class OrderItem(TenantModel):
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=60)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    fifo_cost = models.DecimalField(max_digits=12, decimal_places=2)
    damaged_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)


class StockAllocation(TenantModel):
    item = models.ForeignKey(OrderItem, related_name="allocations", on_delete=models.CASCADE)
    batch = models.ForeignKey("catalog.StockBatch", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)


class TrackingEvent(TenantModel):
    order = models.ForeignKey(Order, related_name="tracking_events", on_delete=models.CASCADE)
    status = models.CharField(max_length=24)
    message = models.CharField(max_length=250, blank=True)
    provider_event_id = models.CharField(max_length=120, unique=True, null=True, blank=True)
    source = models.CharField(max_length=24, default="local")
    occurred_at = models.DateTimeField(null=True, blank=True)
    raw_status = models.TextField(blank=True)
