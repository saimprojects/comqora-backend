from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import F, Sum
from django.utils import timezone
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.response import Response

from apps.accounts.access import locked_payload
from apps.catalog.models import Product, StockBatch
from apps.core.api import TenantViewSet
from apps.core.models import AuditEvent
from apps.marketing.models import Campaign
from apps.orders.models import Customer, Order
from apps.orders.services import financials, money

from .models import Expense


class ExpenseSerializer(serializers.ModelSerializer):
    bank_paid = serializers.SerializerMethodField()
    bank_remaining = serializers.SerializerMethodField()

    def get_bank_paid(self, obj):
        return str(-sum((p.amount for p in obj.payments.all()), Decimal("0")))

    def get_bank_remaining(self, obj):
        return str(obj.amount - Decimal(self.get_bank_paid(obj)))

    class Meta:
        model = Expense
        exclude = ["workspace"]
        read_only_fields = ["id", "created_at", "updated_at"]


class ExpenseViewSet(TenantViewSet):
    queryset = Expense.objects.prefetch_related("payments").all()
    serializer_class = ExpenseSerializer
    http_method_names = ["get", "post", "head", "options"]
    search_fields = ["name", "category"]
    filterset_fields = ["category"]


@api_view(["GET"])
def analytics(request):
    if not request.user.has_dashboard_access:
        return Response(locked_payload(request.user), status=423)
    if not request.user.workspace_id:
        return Response({"detail": "A business workspace is required."}, status=403)
    date_field = serializers.DateField()
    end = date_field.run_validation(request.query_params.get("end", str(timezone.localdate())))
    start = date_field.run_validation(
        request.query_params.get("start", str(end - timedelta(days=29)))
    )
    if start > end or (end - start).days > 366:
        return Response({"detail": "Choose a date range of up to 366 days."}, status=400)
    workspace = request.user.workspace
    orders = list(
        Order.objects.filter(workspace=workspace, created_at__date__range=[start, end])
        .select_related("courier", "customer")
        .prefetch_related("items")
        .order_by("created_at")
    )
    zero = Decimal("0")
    totals = {
        k: zero
        for k in [
            "revenue",
            "delivered_revenue",
            "placed_value",
            "realized_profit",
            "expected_profit",
            "return_loss",
            "costs",
            "ads",
            "final_ads",
            "product",
            "courier",
            "packaging",
            "other",
        ]
    }
    statuses = {key: 0 for key in Order.STATUSES}
    daily = {
        str(start + timedelta(days=i)): {
            "date": str(start + timedelta(days=i)),
            "revenue": zero,
            "profit": zero,
            "orders": 0,
        }
        for i in range((end - start).days + 1)
    }
    products, couriers, cities = {}, {}, {}
    for order in orders:
        f = financials(order)
        statuses[order.status] += 1
        day = daily[str(timezone.localdate(order.created_at))]
        day["orders"] += 1
        totals["ads"] += order.ad_cost
        totals["placed_value"] += order.subtotal - order.discount + order.customer_charges
        if f["is_final"]:
            totals["final_ads"] += order.ad_cost
            revenue, profit = Decimal(f["revenue"]), Decimal(f["profit"])
            totals["revenue"] += revenue
            if order.status == "DELIVERED":
                totals["delivered_revenue"] += revenue
            totals["costs"] += Decimal(f["cost"])
            totals["realized_profit"] += profit
            day["revenue"] += revenue
            day["profit"] += profit
            if order.status in ["RETURNED", "CANCELLED"]:
                totals["return_loss"] += max(zero, -profit)
            for key, value in [
                ("product", f["product_cost"]),
                ("courier", f["courier_cost"]),
                ("packaging", f["packaging_cost"]),
                ("other", order.other_cost),
            ]:
                totals[key] += Decimal(value)
        else:
            totals["expected_profit"] += (
                Decimal(f["expected_profit"]) if order.status != "RETURNED" else zero
            )
        items = list(order.items.all())
        remaining_revenue = Decimal(f["revenue"])
        remaining_shared_cost = Decimal(f["cost"]) - Decimal(f["product_cost"])
        common_cost = remaining_shared_cost
        for index, item in enumerate(items):
            key = str(item.product_id)
            p = products.setdefault(
                key,
                {
                    "id": key,
                    "name": item.name,
                    "sku": item.sku,
                    "units": 0,
                    "revenue": zero,
                    "profit": zero,
                    "returned": 0,
                    "ordered": 0,
                },
            )
            p["ordered"] += item.quantity
            if order.status == "RETURNED":
                p["returned"] += item.quantity
            if f["is_final"]:
                share = item.unit_price * item.quantity / order.subtotal if order.subtotal else zero
                item_revenue = (
                    remaining_revenue
                    if index == len(items) - 1
                    else money(Decimal(f["revenue"]) * share)
                )
                shared_cost = (
                    remaining_shared_cost if index == len(items) - 1 else money(common_cost * share)
                )
                item_product_cost = (
                    item.fifo_cost
                    if order.status == "DELIVERED"
                    else item.damaged_cost
                    if order.status == "RETURNED"
                    else zero
                )
                p["revenue"] += item_revenue
                p["profit"] += item_revenue - shared_cost - item_product_cost
                remaining_revenue -= item_revenue
                remaining_shared_cost -= shared_cost
                if order.status == "DELIVERED":
                    p["units"] += item.quantity
        c = couriers.setdefault(
            str(order.courier_id),
            {
                "name": order.courier_snapshot["courier"],
                "orders": 0,
                "delivered": 0,
                "returned": 0,
                "cost": zero,
                "profit": zero,
                "delivery_days": zero,
            },
        )
        c["orders"] += 1
        if order.dispatched_at:
            c["cost"] += Decimal(f["courier_cost"])
        if f["is_final"]:
            c["profit"] += Decimal(f["profit"])
        if order.status == "DELIVERED":
            c["delivered"] += 1
            c["delivery_days"] += Decimal(
                str((order.finalized_at - order.dispatched_at).total_seconds() / 86400)
            )
        if order.status == "RETURNED":
            c["returned"] += 1
        city = cities.setdefault(
            order.customer_snapshot["city"],
            {"name": order.customer_snapshot["city"], "orders": 0, "delivered": 0},
        )
        city["orders"] += 1
        city["delivered"] += int(order.status == "DELIVERED")
    expenses = (
        Expense.objects.filter(workspace=workspace, date__range=[start, end]).aggregate(
            total=Sum("amount")
        )["total"]
        or zero
    )
    unallocated = (
        Campaign.objects.filter(
            workspace=workspace, allocated=False, start_date__gte=start, start_date__lte=end
        ).aggregate(total=Sum("spend"))["total"]
        or zero
    )
    totals["business_expenses"] = expenses
    totals["unallocated_ads"] = unallocated
    totals["open_order_ads"] = totals["ads"] - totals["final_ads"]
    totals["net_profit"] = (
        totals["realized_profit"] - expenses - unallocated - totals["open_order_ads"]
    )
    totals["margin"] = (
        money(totals["net_profit"] / totals["revenue"] * 100) if totals["revenue"] else zero
    )
    totals["ad_spend"] = totals["ads"] + unallocated
    totals["delivered_roas"] = (
        money(totals["delivered_revenue"] / totals["ad_spend"]) if totals["ad_spend"] else zero
    )
    totals["placed_roas"] = (
        money(totals["placed_value"] / totals["ad_spend"]) if totals["ad_spend"] else zero
    )
    totals["profit_roas"] = (
        money(totals["net_profit"] / totals["ad_spend"]) if totals["ad_spend"] else zero
    )
    totals["cost_per_order"] = money(totals["ad_spend"] / len(orders)) if orders else zero
    totals["cost_per_delivery"] = (
        money(totals["ad_spend"] / statuses["DELIVERED"]) if statuses["DELIVERED"] else zero
    )
    stock = Product.objects.filter(workspace=workspace, is_active=True).annotate(
        available=Sum(F("batches__remaining_quantity") - F("batches__reserved_quantity"), default=0)
    )
    low = [
        {"id": p.pk, "name": p.name, "available": p.available}
        for p in stock
        if p.available <= p.low_stock_threshold
    ]
    inventory_value = sum(
        (
            b.unit_cost * b.remaining_quantity
            for b in StockBatch.objects.filter(workspace=workspace)
        ),
        zero,
    )
    recent = sorted(orders, key=lambda o: o.created_at, reverse=True)[:6]
    from apps.orders.api import OrderSerializer

    customer_ids = {o.customer_id for o in orders}
    counts = defaultdict(int)
    for cid in Order.objects.filter(workspace=workspace, customer_id__in=customer_ids).values_list(
        "customer_id", flat=True
    ):
        counts[cid] += 1
    return Response(
        {
            "period": {
                "start": start,
                "end": end,
                "basis": "Order creation cohort; final outcomes recognize revenue. All in-scope ads are expensed, including open-order ads. Business expenses by expense date; unallocated campaigns by start date.",
            },
            "totals": totals,
            "statuses": statuses,
            "order_count": len(orders),
            "daily": list(daily.values()),
            "products": sorted(products.values(), key=lambda p: p["profit"], reverse=True),
            "couriers": list(couriers.values()),
            "locations": list(cities.values()),
            "low_stock": low,
            "inventory_value": inventory_value,
            "customers": {
                "active": len(customer_ids),
                "repeat": sum(v > 1 for v in counts.values()),
                "new": Customer.objects.filter(
                    workspace=workspace, created_at__date__range=[start, end]
                ).count(),
            },
            "recent_orders": OrderSerializer(recent, many=True).data,
            "pending_returns": sum(
                1 for o in orders if o.status == "RETURNED" and not o.return_received_at
            ),
        }
    )


@api_view(["GET"])
def activity(request):
    if not request.user.has_dashboard_access:
        return Response(locked_payload(request.user), status=423)
    if not request.user.workspace_id:
        return Response([], status=403)
    events = AuditEvent.objects.filter(workspace=request.user.workspace).select_related("actor")[
        :30
    ]
    return Response(
        [
            {
                "id": e.pk,
                "action": e.action,
                "actor": e.actor.first_name if e.actor else "System",
                "created_at": e.created_at,
                "detail": e.detail,
            }
            for e in events
        ]
    )
