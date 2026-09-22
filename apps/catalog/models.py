from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

from apps.core.models import TenantModel


class Category(TenantModel):
    name = models.CharField(max_length=80)

    class Meta(TenantModel.Meta):
        constraints = [
            models.UniqueConstraint(
                models.functions.Lower("name"), "workspace", name="unique_workspace_category"
            )
        ]

    def __str__(self):
        return self.name


class Product(TenantModel):
    name = models.CharField(max_length=150)
    sku = models.CharField(max_length=60)
    category = models.CharField(max_length=80, default="General")
    category_record = models.ForeignKey(
        Category, null=True, blank=True, on_delete=models.PROTECT, related_name="products"
    )
    variant = models.CharField(max_length=80, blank=True)
    description = models.TextField(blank=True)
    selling_price = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    image_url = models.URLField(blank=True)
    low_stock_threshold = models.PositiveIntegerField(default=10)
    is_active = models.BooleanField(default=True)

    class Meta(TenantModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["workspace", "sku"], name="unique_workspace_sku")
        ]

    def __str__(self):
        return f"{self.name} ({self.sku})"


class StockBatch(TenantModel):
    product = models.ForeignKey(Product, related_name="batches", on_delete=models.PROTECT)
    reference = models.CharField(max_length=80)
    purchased_quantity = models.PositiveIntegerField()
    remaining_quantity = models.PositiveIntegerField()
    reserved_quantity = models.PositiveIntegerField(default=0)
    unit_cost = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(0)]
    )
    received_at = models.DateField()
    purchase_mode = models.CharField(
        max_length=8, choices=[("UNIT", "Per unit"), ("TOTAL", "Batch total")], default="UNIT"
    )
    purchase_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    transport_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    import_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    extra_costs = models.JSONField(default=list, blank=True)

    class Meta(TenantModel.Meta):
        ordering = ["received_at", "created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(reserved_quantity__lte=models.F("remaining_quantity")),
                name="reserved_within_stock",
            )
        ]


class Packaging(TenantModel):
    name = models.CharField(max_length=100)
    unit = models.CharField(
        max_length=20,
        choices=[("piece", "Piece"), ("meter", "Meter"), ("gram", "Gram")],
        default="piece",
    )
    unit_cost = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(0)]
    )
    stock = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    default_quantity = models.DecimalField(
        max_digits=8, decimal_places=2, default=1, validators=[MinValueValidator(0)]
    )

    def __str__(self):
        return self.name
