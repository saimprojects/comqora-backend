"""Recalculate historic estimates using the rates saved on each order."""

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations
from django.utils import timezone


BASIS = "DISCOUNTED_PRODUCT_TOTAL"


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def recalculate(apps, alias, workspace_id=None):
    Order = apps.get_model("orders", "Order")
    AuditEvent = apps.get_model("core", "AuditEvent")
    orders = Order.objects.using(alias).order_by("pk")
    if workspace_id is not None:
        orders = orders.filter(workspace_id=workspace_id)
    updated = 0
    for order in orders.select_for_update().iterator():
        before = order.courier_snapshot
        if before.get("percentage_basis_type") == BASIS:
            continue
        fees = before.get("extra_fees", [])
        if not Decimal(str(before.get("tax_percent", 0))) and not any(
            fee.get("kind") == "PERCENT" for fee in fees
        ):
            continue
        basis = money(order.subtotal - order.discount)
        if basis < 0:
            raise ValueError(f"Negative discounted product total on order {order.pk}")
        snapshot = deepcopy(before)
        snapshot["percentage_basis"] = str(basis)
        snapshot["percentage_basis_type"] = BASIS
        snapshot["tax"] = str(money(basis * Decimal(str(before.get("tax_percent", 0))) / 100))
        snapshot["extra_fees"] = [
            {
                **fee,
                "cost": str(
                    money(
                        basis * Decimal(str(fee["amount"])) / 100
                        if fee["kind"] == "PERCENT"
                        else fee["amount"]
                    )
                ),
            }
            for fee in fees
        ]
        total = money(
            Decimal(str(before["base_rate"]))
            + Decimal(str(before.get("extra_weight_charge", 0)))
            + Decimal(snapshot["tax"])
            + Decimal(str(before.get("fixed_charge", 0)))
            + sum((Decimal(fee["cost"]) for fee in snapshot["extra_fees"]), Decimal(0))
        )
        charges = order.customer_charges
        if order.charges_mode == "ADD":
            charges = money(charges + total - order.courier_cost)
        if not (
            0 <= total <= Decimal("9999999999.99") and 0 <= charges <= Decimal("9999999999.99")
        ):
            raise ValueError(f"Recalculated charges exceed supported amounts on order {order.pk}")
        snapshot["total"] = str(total)
        AuditEvent.objects.using(alias).create(
            workspace_id=order.workspace_id,
            action="order.courier_percentages_recalculated",
            object_id=str(order.pk),
            detail={
                "before": {
                    "courier_snapshot": before,
                    "courier_cost": str(order.courier_cost),
                    "customer_charges": str(order.customer_charges),
                },
                "after": {
                    "courier_snapshot": snapshot,
                    "courier_cost": str(total),
                    "customer_charges": str(charges),
                },
            },
        )
        # Actual settlements, payments, refunds and stock are independent records.
        Order.objects.using(alias).filter(pk=order.pk).update(
            courier_snapshot=snapshot,
            courier_cost=total,
            customer_charges=charges,
            updated_at=timezone.now(),
        )
        updated += 1
    return updated


def forwards(apps, schema_editor):
    recalculate(apps, schema_editor.connection.alias)


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0008_order_actual_courier_cost_and_more"),
        ("core", "0001_initial"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
