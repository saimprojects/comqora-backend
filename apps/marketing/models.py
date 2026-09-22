from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

from apps.core.models import TenantModel


class Campaign(TenantModel):
    name = models.CharField(max_length=120)
    channel = models.CharField(
        max_length=30,
        choices=[("Meta", "Meta"), ("Google", "Google"), ("TikTok", "TikTok"), ("Other", "Other")],
        default="Meta",
    )
    spend = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    start_date = models.DateField()
    end_date = models.DateField()
    allocated = models.BooleanField(default=False)


class AdAllocation(TenantModel):
    campaign = models.ForeignKey(Campaign, related_name="allocations", on_delete=models.PROTECT)
    order = models.ForeignKey(
        "orders.Order", related_name="ad_allocations", on_delete=models.PROTECT
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    active = models.BooleanField(default=True)
