"""Financial and stock mutations live here, inside database transactions."""

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from uuid import uuid4

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Packaging, Product, StockBatch
from apps.core.models import Workspace
from apps.logistics.models import Courier

from .models import Order, OrderItem, StockAllocation, TrackingEvent


def notify_order(order, event, key):
    from apps.messaging.services import enqueue_order

    enqueue_order(order, event, key)


ZERO = Decimal("0.00")


def money(value):
    return Decimal(str(value)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)


def quote(courier, weight, zone="OUTSIDE_PROVINCE"):
    base = courier.base_rate
    if courier.provincial_pricing:
        base = (
            courier.outside_province_rate
            if zone == "OUTSIDE_PROVINCE"
            else courier.same_province_rate
        )
    if courier.city_pricing and zone == "SAME_CITY":
        base = courier.same_city_rate
    extra = max(ZERO, Decimal(weight) - courier.base_weight).to_integral_value(
        rounding=ROUND_CEILING
    )
    transport = base + extra * courier.additional_kg_rate
    tax = money(transport * courier.tax_percent / 100)
    fees = [
        {
            **fee,
            "cost": str(
                money(
                    transport * Decimal(fee["amount"]) / 100
                    if fee["kind"] == "PERCENT"
                    else fee["amount"]
                )
            ),
        }
        for fee in courier.extra_fees
    ]
    total = money(
        transport + tax + courier.fixed_charge + sum((money(f["cost"]) for f in fees), ZERO)
    )
    if total > Decimal("9999999999.99"):
        raise ValidationError("Shipping total exceeds the supported amount.")
    return {
        "courier": courier.name,
        "provider": courier.provider,
        "base_rate": str(base),
        "delivery_zone": zone,
        "extra_fees": fees,
        "percentage_basis": str(transport),
        "extra_weight_charge": str(extra * courier.additional_kg_rate),
        "tax_percent": str(courier.tax_percent),
        "tax": str(tax),
        "fixed_charge": str(courier.fixed_charge),
        "total": str(total),
        "return_rate": str(courier.return_rate),
    }


def financials(order):
    sale = order.subtotal - order.discount + order.customer_charges
    returning = order.status == "RETURNED"
    estimate_product = ZERO if returning else order.product_cost
    estimate_shipping = order.courier_cost + (order.return_cost if returning else ZERO)
    original_shipping = estimate_shipping
    if order.actual_courier_cost is not None:
        estimate_shipping = order.actual_courier_cost
    estimate_revenue = (
        max(ZERO, order.advance_paid - order.refunded_amount)
        if returning
        else sale - order.refunded_amount
    )
    estimate_cost = (
        estimate_product
        + order.ad_cost
        + estimate_shipping
        + order.packaging_cost
        + order.other_cost
    )
    terminal = order.status in ["DELIVERED", "CANCELLED"] or (
        order.status == "RETURNED" and order.return_received_at is not None
    )
    revenue = (
        sale - order.refunded_amount
        if order.status == "DELIVERED"
        else max(ZERO, order.advance_paid - order.refunded_amount)
        if terminal
        else ZERO
    )
    product = (
        order.product_cost
        if order.status == "DELIVERED"
        else order.damaged_cost
        if order.status == "RETURNED"
        else ZERO
    )
    shipping = order.courier_cost + order.return_cost if order.dispatched_at else ZERO
    if order.actual_courier_cost is not None:
        shipping = order.actual_courier_cost
    packaging = order.packaging_cost if order.dispatched_at else ZERO
    cost = product + shipping + packaging + order.ad_cost + order.other_cost
    profit = revenue - cost if terminal else ZERO
    state = "REALIZED" if order.status == "DELIVERED" else "LOSS" if terminal else "ESTIMATED"
    return {
        "state": state,
        "net_sale": str(sale),
        "revenue": str(revenue),
        "expected_profit": str(estimate_revenue - estimate_cost),
        "profit": str(profit),
        "cost": str(cost if terminal else estimate_cost),
        "product_cost": str(product if terminal else estimate_product),
        "courier_cost": str(shipping if terminal else estimate_shipping),
        "courier_cost_estimate": str(original_shipping),
        "courier_cost_basis": order.actual_courier_cost_basis or "ESTIMATED",
        "courier_cost_source": str(order.actual_courier_cost_source or ""),
        "packaging_cost": str(packaging if terminal else order.packaging_cost),
        "margin": str(money(profit / revenue * 100)) if revenue > 0 and terminal else "0.00",
        "is_final": terminal,
    }


@transaction.atomic
def create_order(workspace, data):
    # Workspace lock also serializes order creation and campaign allocation in a tenant.
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    entries = data.pop("items")
    packs = data.pop("packaging", [])
    other = data.pop("other_costs", [])
    other_total = sum((c["amount"] for c in other), ZERO)
    if other_total > Decimal("9999999999.99"):
        raise ValidationError({"other_costs": "Combined other costs exceed the supported amount."})
    products = {
        str(p.pk): p
        for p in Product.objects.select_for_update()
        .filter(workspace=workspace, is_active=True, pk__in=[e["product"] for e in entries])
        .order_by("pk")
    }
    courier = Courier.objects.get(pk=data.pop("courier"), workspace=workspace, is_active=True)
    snapshot = quote(courier, data["weight"], data.get("delivery_zone", "OUTSIDE_PROVINCE"))
    customer = data.pop("customer")
    order = Order.objects.create(
        workspace=workspace,
        number="SF-" + uuid4().hex[:8].upper(),
        customer=customer,
        customer_snapshot={
            "name": customer.name,
            "phone": customer.phone,
            "city": customer.city,
            "province": customer.province,
            "address": customer.address,
        },
        courier=courier,
        courier_snapshot=snapshot,
        courier_cost=money(snapshot["total"]),
        subtotal=0,
        other_costs=[{"name": c["name"], "amount": str(c["amount"])} for c in other],
        other_cost=other_total,
        **data,
    )
    total, cost = ZERO, ZERO
    for entry in entries:
        product = products.get(str(entry["product"]))
        if not product:
            raise ValidationError({"items": "Product is inactive or outside this workspace."})
        quantity = entry["quantity"]
        price = entry.get("unit_price", product.selling_price)
        item = OrderItem.objects.create(
            workspace=workspace,
            order=order,
            product=product,
            name=product.name,
            sku=product.sku,
            quantity=quantity,
            unit_price=price,
            fifo_cost=0,
        )
        needed, item_cost = quantity, ZERO
        for batch in (
            StockBatch.objects.select_for_update()
            .filter(workspace=workspace, product=product)
            .order_by("received_at", "created_at", "id")
        ):
            available = batch.remaining_quantity - batch.reserved_quantity
            take = min(needed, available)
            if take <= 0:
                continue
            batch.reserved_quantity += take
            batch.save(update_fields=["reserved_quantity"])
            StockAllocation.objects.create(
                workspace=workspace,
                item=item,
                batch=batch,
                quantity=take,
                unit_cost=batch.unit_cost,
            )
            item_cost += take * batch.unit_cost
            needed -= take
            if needed == 0:
                break
        if needed:
            raise ValidationError({"items": f"Insufficient available stock for {product.name}."})
        item.fifo_cost = item_cost
        item.save(update_fields=["fifo_cost"])
        total += price * quantity
        cost += item_cost
    if order.discount > total:
        raise ValidationError({"discount": "Discount cannot exceed the subtotal."})
    seen = set()
    for pack in packs:
        if pack["id"] in seen:
            raise ValidationError(
                {"packaging": "Combine duplicate packaging into a single quantity."}
            )
        seen.add(pack["id"])
        packaging = Packaging.objects.filter(pk=pack["id"], workspace=workspace).first()
        if not packaging:
            raise ValidationError({"packaging": "Packaging not found."})
        order.packaging_snapshot.append(
            {
                "id": str(packaging.pk),
                "name": packaging.name,
                "unit": packaging.unit,
                "unit_cost": str(packaging.unit_cost),
                "quantity": str(pack["quantity"]),
                "cost": str(money(packaging.unit_cost * pack["quantity"])),
            }
        )
    order.packaging_cost = sum((money(p["cost"]) for p in order.packaging_snapshot), ZERO)
    if order.charges_mode == "ADD":
        order.customer_charges = money(
            order.courier_cost + order.packaging_cost + order.ad_cost + order.other_cost
        )
    net = total - order.discount + order.customer_charges
    if max(total, net, cost, order.packaging_cost, order.customer_charges) > Decimal(
        "9999999999.99"
    ):
        raise ValidationError("Order total exceeds the supported amount.")
    if (
        order.advance_paid > net
        or (order.payment_type == "COD" and order.advance_paid != 0)
        or (order.payment_type == "PREPAID" and order.advance_paid != net)
    ):
        raise ValidationError(
            {
                "advance_paid": "COD requires zero advance; prepaid requires full net sale; advance cannot exceed net sale."
            }
        )
    order.subtotal, order.product_cost = total, cost
    order.save()
    TrackingEvent.objects.create(
        workspace=workspace,
        order=order,
        status="CREATED",
        message="Order created. FIFO stock reserved.",
    )
    notify_order(order, "CREATED", "created")
    return order


def locked_allocations(order):
    # Lock product rows in stable order across create, dispatch, cancel and return.
    list(
        Product.objects.select_for_update()
        .filter(pk__in=order.items.values("product_id"))
        .order_by("pk")
    )
    return (
        StockAllocation.objects.filter(item__order=order)
        .select_related("item")
        .order_by("batch_id")
    )


@transaction.atomic
def dispatch_order(order_id, workspace, tracking_id):
    order = Order.objects.select_for_update().get(pk=order_id, workspace=workspace)
    if order.status != "CREATED":
        raise ValidationError("Only newly created orders can be dispatched.")
    if not tracking_id or len(tracking_id.strip()) > 100:
        raise ValidationError("A valid courier tracking ID is required.")
    for allocation in locked_allocations(order):
        batch = StockBatch.objects.select_for_update().get(pk=allocation.batch_id)
        batch.remaining_quantity -= allocation.quantity
        batch.reserved_quantity -= allocation.quantity
        batch.save(update_fields=["remaining_quantity", "reserved_quantity"])
    for p in sorted(order.packaging_snapshot, key=lambda p: p["id"]):
        packaging = Packaging.objects.select_for_update().get(pk=p["id"], workspace=workspace)
        quantity = Decimal(p["quantity"])
        if packaging.stock < quantity:
            raise ValidationError(f"Insufficient packaging stock: {packaging.name}.")
        packaging.stock -= quantity
        packaging.save(update_fields=["stock"])
    order.tracking_id, order.status, order.dispatched_at = (
        tracking_id.strip(),
        "IN_TRANSIT",
        timezone.now(),
    )
    order.tracking_provider = order.courier_snapshot.get("provider", order.courier.provider)
    order.tracking_next_sync_at = timezone.now() if order.tracking_provider != "Others" else None
    order.save()
    TrackingEvent.objects.create(
        workspace=workspace,
        order=order,
        status="IN_TRANSIT",
        message="Dispatched. Awaiting courier tracking events.",
    )
    notify_order(order, "IN_TRANSIT", "dispatch")
    return order


@transaction.atomic
def cancel_order(order_id, workspace):
    order = Order.objects.select_for_update().get(pk=order_id, workspace=workspace)
    if order.status != "CREATED":
        raise ValidationError("Only orders not yet dispatched can be cancelled.")
    for allocation in locked_allocations(order):
        batch = StockBatch.objects.select_for_update().get(pk=allocation.batch_id)
        batch.reserved_quantity -= allocation.quantity
        batch.save(update_fields=["reserved_quantity"])
    order.status, order.finalized_at = "CANCELLED", timezone.now()
    order.save()
    TrackingEvent.objects.create(
        workspace=workspace,
        order=order,
        status="CANCELLED",
        message="Cancelled before dispatch; reservations released.",
    )
    notify_order(order, "CANCELLED", "cancel")
    return order


@transaction.atomic
def apply_tracking(order_id, workspace, status, event_id, message="", *, manual=False):
    order = Order.objects.select_for_update().get(pk=order_id, workspace=workspace)
    previous_status = order.status
    if order.tracking_mode == "MANUAL" and not manual:
        raise ValidationError("Automatic updates are paused for this order.")
    existing = TrackingEvent.objects.filter(provider_event_id=event_id).first()
    if existing:
        if existing.order_id != order.pk or existing.status != status:
            raise ValidationError("This event ID was already used for a different event.")
        return order
    if order.status not in Order.ACTIVE_SHIPMENT_STATUSES or status not in Order.TRACKING_STATUSES:
        raise ValidationError("Invalid or out-of-order courier transition.")
    order.status = status
    order.tracking_status_at = timezone.now()
    if manual:
        order.tracking_mode = "MANUAL"
        order.tracking_next_sync_at = None
        order.tracking_lock_token = None
        order.tracking_lock_until = None
        order.tracking_error = ""
    if status == "DELIVERED":
        order.finalized_at = timezone.now()
    if status == "RETURNED":
        order.return_cost = money(order.courier_snapshot["return_rate"])
    order.save()
    TrackingEvent.objects.create(
        workspace=workspace,
        order=order,
        status=status,
        message=message[:250],
        provider_event_id=event_id,
        source="manual" if manual else "webhook",
        occurred_at=timezone.now(),
    )
    if previous_status != status:
        notify_order(order, status, event_id)
    return order


@transaction.atomic
def receive_return(order_id, workspace, damaged):
    order = Order.objects.select_for_update().get(pk=order_id, workspace=workspace)
    if order.status != "RETURNED" or order.return_received_at:
        raise ValidationError("Return must be confirmed by the courier and not already received.")
    quantities = {str(i.pk): i.quantity for i in order.items.all()}
    if any(
        key not in quantities or qty > quantities[key] or qty < 0 for key, qty in damaged.items()
    ):
        raise ValidationError(
            "Damaged quantities must match this order and cannot exceed shipped quantities."
        )
    cost = ZERO
    item_damage = {}
    for allocation in locked_allocations(order):
        key = str(allocation.item_id)
        loss = min(damaged.get(key, 0), allocation.quantity)
        damaged[key] = damaged.get(key, 0) - loss
        batch = StockBatch.objects.select_for_update().get(pk=allocation.batch_id)
        batch.remaining_quantity += allocation.quantity - loss
        batch.save(update_fields=["remaining_quantity"])
        cost += loss * allocation.unit_cost
        item_damage[key] = item_damage.get(key, ZERO) + loss * allocation.unit_cost
    for item_id, amount in item_damage.items():
        OrderItem.objects.filter(pk=item_id, order=order).update(damaged_cost=amount)
    order.damaged_cost = cost
    order.return_received_at = order.finalized_at = timezone.now()
    order.save()
    TrackingEvent.objects.create(
        workspace=workspace,
        order=order,
        status="RETURNED",
        message="Return inspected. Reusable units restocked at original FIFO cost.",
    )
    notify_order(order, "RETURN_RECEIVED", "received")
    return order
