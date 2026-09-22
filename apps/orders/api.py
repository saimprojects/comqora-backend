import csv
from decimal import Decimal
from io import StringIO

from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.api import TenantViewSet, audit
from apps.logistics.models import Courier

from .models import Customer, Order, OrderItem, StockAllocation, TrackingEvent
from .services import cancel_order, create_order, dispatch_order, financials, receive_return


class CustomerSerializer(serializers.ModelSerializer):
    order_count = serializers.IntegerField(read_only=True)
    returned_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Customer
        exclude = ["workspace"]
        read_only_fields = ["id", "created_at", "updated_at"]


class CustomerViewSet(TenantViewSet):
    queryset = Customer.objects.annotate(
        order_count=Count("orders"),
        returned_count=Count("orders", filter=Q(orders__status="RETURNED")),
    )
    serializer_class = CustomerSerializer
    write_roles = ["owner", "manager", "staff"]
    search_fields = ["name", "phone", "email", "city", "province", "address"]
    ordering_fields = ["name", "created_at"]
    ordering = ["-created_at", "-id"]


class AllocationSerializer(serializers.ModelSerializer):
    reference = serializers.CharField(source="batch.reference", read_only=True)

    class Meta:
        model = StockAllocation
        fields = ["id", "reference", "quantity", "unit_cost"]


class ItemSerializer(serializers.ModelSerializer):
    allocations = AllocationSerializer(many=True, read_only=True)

    class Meta:
        model = OrderItem
        fields = [
            "id",
            "product",
            "name",
            "sku",
            "quantity",
            "unit_price",
            "fifo_cost",
            "damaged_cost",
            "allocations",
        ]


class EventSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrackingEvent
        fields = ["id", "status", "message", "created_at", "source", "occurred_at", "raw_status"]


class OrderSerializer(serializers.ModelSerializer):
    tracking = serializers.SerializerMethodField()

    def get_tracking(self, obj):
        from apps.logistics.tracking import tracking_info

        return tracking_info(obj)

    customer_name = serializers.CharField(source="customer_snapshot.name", read_only=True)
    city = serializers.CharField(source="customer_snapshot.city", read_only=True)
    courier_name = serializers.CharField(source="courier_snapshot.courier", read_only=True)
    financials = serializers.SerializerMethodField()
    items = ItemSerializer(many=True, read_only=True)
    tracking_events = EventSerializer(many=True, read_only=True)
    ad_history = serializers.SerializerMethodField()

    def get_financials(self, obj):
        return financials(obj)

    def get_ad_history(self, obj):
        return [
            {
                "campaign": a.campaign.name,
                "amount": str(a.amount),
                "active": a.active,
                "created_at": a.created_at,
            }
            for a in obj.ad_allocations.all()
        ]

    class Meta:
        model = Order
        exclude = ["workspace", "tracking_lock_token", "tracking_lock_until"]


class NewItem(serializers.Serializer):
    product = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1, max_value=100000)
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal(".01"), required=False
    )


class NewPackaging(serializers.Serializer):
    id = serializers.UUIDField()
    quantity = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal(".01"))


class OtherCost(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)


class CreateOrderSerializer(serializers.Serializer):
    customer = serializers.PrimaryKeyRelatedField(queryset=Customer.objects.all())
    courier = serializers.UUIDField()
    weight = serializers.DecimalField(
        max_digits=8, decimal_places=2, min_value=Decimal(".01"), default=Decimal("0.5")
    )
    payment_type = serializers.ChoiceField(choices=["COD", "PREPAID", "PARTIAL"], default="COD")
    delivery_zone = serializers.ChoiceField(
        choices=["SAME_CITY", "SAME_PROVINCE", "OUTSIDE_PROVINCE"], default="OUTSIDE_PROVINCE"
    )
    charges_mode = serializers.ChoiceField(choices=["ABSORB", "ADD"], default="ABSORB")
    advance_paid = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, default=Decimal("0")
    )
    discount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, default=Decimal("0")
    )
    ad_cost = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, default=Decimal("0")
    )
    notes = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    items = NewItem(many=True, allow_empty=False)
    packaging = NewPackaging(many=True, required=False)
    other_costs = OtherCost(many=True, required=False, max_length=30)

    def validate_customer(self, customer):
        if customer.workspace_id != self.context["request"].user.workspace_id:
            raise serializers.ValidationError("Customer not found.")
        return customer

    def validate_courier(self, courier):
        if not Courier.objects.filter(
            pk=courier, workspace=self.context["request"].user.workspace, is_active=True
        ).exists():
            raise serializers.ValidationError("Active courier not found.")
        return courier


class OrderViewSet(TenantViewSet):
    queryset = Order.objects.select_related("customer", "courier", "workspace").prefetch_related(
        "items__allocations__batch", "tracking_events", "ad_allocations__campaign"
    )
    serializer_class = OrderSerializer
    write_roles = ["owner", "manager", "staff"]
    http_method_names = ["get", "post", "head", "options"]
    search_fields = ["number", "customer__name", "tracking_id", "customer__phone"]
    filterset_fields = ["status", "customer", "courier", "payment_type"]
    ordering_fields = ["created_at", "subtotal", "number"]

    def create(self, request, *args, **kwargs):
        serializer = CreateOrderSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        order = create_order(request.user.workspace, serializer.validated_data)
        audit(request, "order.created", order)
        return Response(OrderSerializer(order).data, status=201)

    @action(detail=True, methods=["post"], url_path="dispatch")
    def dispatch_shipment(self, request, pk=None):
        self.get_object()
        field = serializers.CharField(max_length=100)
        tracking_id = field.run_validation(request.data.get("tracking_id", ""))
        order = dispatch_order(pk, request.user.workspace, tracking_id)
        audit(request, "order.dispatched", order)
        return Response(OrderSerializer(order).data)

    @action(detail=True, methods=["post"], url_path="sync-tracking")
    def sync_tracking(self, request, pk=None):
        from apps.logistics.providers import TrackingError
        from apps.logistics.tracking import sync_order

        self.get_object()
        try:
            result = sync_order(pk, workspace_id=request.user.workspace_id)
        except TrackingError as exc:
            return Response({"detail": str(exc)}, status=502)
        return Response(result)

    @action(detail=True, methods=["post"], url_path="manual-status")
    @transaction.atomic
    def manual_status(self, request, pk=None):
        from uuid import uuid4

        from .services import apply_tracking

        self.get_object()
        status = serializers.ChoiceField(choices=Order.TRACKING_STATUSES).run_validation(
            request.data.get("status")
        )
        message = serializers.CharField(max_length=250, min_length=3).run_validation(
            request.data.get("message")
        )
        order = apply_tracking(
            pk, request.user.workspace, status, "manual:" + uuid4().hex, message, manual=True
        )
        audit(request, "order.manual_status", order, {"status": status, "reason": message})
        return Response(OrderSerializer(order).data)

    @action(detail=True, methods=["post"], url_path="resume-tracking")
    @transaction.atomic
    def resume_tracking(self, request, pk=None):
        from django.utils import timezone

        from apps.logistics.tracking import tracking_info

        self.get_object()
        order = Order.objects.select_for_update().get(pk=pk, workspace=request.user.workspace)
        if (
            order.status not in Order.ACTIVE_SHIPMENT_STATUSES
            or not tracking_info(order)["configured"]
        ):
            raise serializers.ValidationError(
                "Only supported, configured, unresolved shipments can resume automatic tracking."
            )
        order.tracking_mode = "AUTO"
        order.tracking_next_sync_at = timezone.now()
        order.tracking_lock_token = None
        order.tracking_lock_until = None
        order.save()
        TrackingEvent.objects.create(
            workspace=request.user.workspace,
            order=order,
            status=order.status,
            source="manual",
            occurred_at=timezone.now(),
            message="Automatic tracking resumed by a team member.",
        )
        audit(request, "order.tracking_resumed", order)
        return Response(OrderSerializer(order).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        self.get_object()
        order = cancel_order(pk, request.user.workspace)
        audit(request, "order.cancelled", order)
        return Response(OrderSerializer(order).data)

    @action(detail=True, methods=["post"], url_path="receive-return")
    def receive(self, request, pk=None):
        self.get_object()
        damaged = serializers.DictField(child=serializers.IntegerField(min_value=0)).run_validation(
            request.data.get("damaged", {})
        )
        order = receive_return(pk, request.user.workspace, damaged)
        audit(request, "order.return_received", order)
        return Response(OrderSerializer(order).data)

    @action(detail=True, methods=["post"])
    def refund(self, request, pk=None):
        if request.user.role not in ["owner", "manager"]:
            return Response({"detail": "Only owners and managers can record refunds."}, status=403)
        self.get_object()
        amount = serializers.DecimalField(
            max_digits=12, decimal_places=2, min_value=Decimal(".01")
        ).run_validation(request.data.get("amount"))
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=pk, workspace=request.user.workspace)
            limit = (
                order.subtotal - order.discount + order.customer_charges
                if order.status == "DELIVERED"
                else order.advance_paid
            )
            if order.refunded_amount + amount > limit:
                return Response({"detail": "Refund cannot exceed collected revenue."}, status=400)
            order.refunded_amount += amount
            order.save(update_fields=["refunded_amount"])
            audit(request, "order.refund_recorded", order, {"amount": str(amount)})
            from .services import notify_order

            notify_order(order, "REFUND_RECORDED", str(order.refunded_amount))
        return Response(OrderSerializer(order).data)

    @action(detail=False, methods=["get"])
    def export(self, request):
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(
            ["Order", "Customer", "City", "Status", "Net sale", "Revenue", "Profit", "State"]
        )

        def safe(value):
            text = str(value)
            return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text

        for order in self.filter_queryset(self.get_queryset()).iterator(chunk_size=250):
            f = financials(order)
            writer.writerow(
                [
                    safe(order.number),
                    safe(order.customer_snapshot["name"]),
                    safe(order.customer_snapshot["city"]),
                    order.status,
                    f["net_sale"],
                    f["revenue"],
                    f["profit"],
                    f["state"],
                ]
            )
        response = HttpResponse(output.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="sellflow-orders.csv"'
        return response
