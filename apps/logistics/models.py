from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

from apps.core.models import TenantModel


class Courier(TenantModel):
    PROVIDERS = [
        (x, x)
        for x in [
            "TCS",
            "Leopards",
            "M&P",
            "Trax",
            "Daewoo",
            "Dastaq Logistic",
            "AHL",
            "PostEx",
            "Others",
        ]
    ]
    name = models.CharField(max_length=80)
    provider = models.CharField(max_length=24, choices=PROVIDERS, default="Others")
    code = models.SlugField(max_length=50)
    base_weight = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.5"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    base_rate = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(0)]
    )
    additional_kg_rate = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    tax_percent = models.DecimalField(
        max_digits=6, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    fixed_charge = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    return_rate = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    is_active = models.BooleanField(default=True)
    extra_fees = models.JSONField(default=list, blank=True)
    provincial_pricing = models.BooleanField(default=False)
    same_province_rate = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    outside_province_rate = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    city_pricing = models.BooleanField(default=False)
    same_city_rate = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )

    class Meta(TenantModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["workspace", "code"], name="unique_workspace_courier")
        ]

    def __str__(self):
        return self.name


class TrackingWorkerState(models.Model):
    name = models.CharField(max_length=30, primary_key=True)
    heartbeat = models.DateTimeField()
