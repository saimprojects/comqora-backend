import hashlib
import hmac
import time
from decimal import Decimal

from django.conf import settings
from rest_framework import serializers
from rest_framework.decorators import action, api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.core.api import TenantViewSet
from apps.core.pricing import CourierFeeSerializer, json_costs
from apps.orders.models import Order
from apps.orders.services import apply_tracking, quote

from .models import Courier


class CourierSerializer(serializers.ModelSerializer):
    extra_fees = CourierFeeSerializer(many=True, required=False, max_length=30)

    def validate_extra_fees(self, value):
        return json_costs(value)

    def validate(self, data):
        for flag, fields in [
            ("provincial_pricing", ["same_province_rate", "outside_province_rate"]),
            ("city_pricing", ["same_city_rate"]),
        ]:
            enabled = data.get(flag, getattr(self.instance, flag, False))
            if enabled and (not self.instance or not getattr(self.instance, flag)):
                for field in fields:
                    if field not in data:
                        raise serializers.ValidationError(
                            {field: "Enter a rate when enabling regional pricing."}
                        )
        return data

    auto_tracking = serializers.SerializerMethodField()

    def get_auto_tracking(self, obj):
        return obj.provider != "Others"

    class Meta:
        model = Courier
        exclude = ["workspace"]
        read_only_fields = ["id", "created_at", "updated_at"]
        validators = []

    def validate_code(self, value):
        qs = Courier.objects.filter(workspace=self.context["request"].user.workspace, code=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("Courier code already exists.")
        return value


class CourierViewSet(TenantViewSet):
    queryset = Courier.objects.all()
    serializer_class = CourierSerializer
    search_fields = ["name", "code"]

    @action(detail=True, methods=["get"])
    def quote(self, request, pk=None):
        field = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal(".01"))
        weight = field.run_validation(request.query_params.get("weight", "0.5"))
        zone = serializers.ChoiceField(
            choices=["SAME_CITY", "SAME_PROVINCE", "OUTSIDE_PROVINCE"]
        ).run_validation(request.query_params.get("zone", "OUTSIDE_PROVINCE"))
        return Response(quote(self.get_object(), weight, zone))


class WebhookPayload(serializers.Serializer):
    order_id = serializers.UUIDField()
    workspace_id = serializers.UUIDField()
    event_id = serializers.CharField(max_length=120)
    status = serializers.ChoiceField(choices=Order.TRACKING_STATUSES)
    message = serializers.CharField(max_length=250, required=False, allow_blank=True)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def tracking_webhook(request):
    secret = settings.TRACKING_WEBHOOK_SECRET
    if not secret:
        return Response({"detail": "Tracking integration is not configured."}, status=503)
    timestamp = request.headers.get("X-SellFlow-Timestamp", "")
    try:
        if abs(time.time() - int(timestamp)) > 300:
            raise ValueError()
    except ValueError:
        return Response({"detail": "Expired webhook timestamp."}, status=401)
    expected = hmac.new(
        secret.encode(), timestamp.encode() + b"." + request.body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("X-SellFlow-Signature", "")):
        return Response({"detail": "Invalid webhook signature."}, status=401)
    serializer = WebhookPayload(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    order = Order.objects.filter(pk=data["order_id"], workspace_id=data["workspace_id"]).first()
    if not order:
        return Response({"detail": "Order not found."}, status=404)
    apply_tracking(
        order.pk, order.workspace, data["status"], data["event_id"], data.get("message", "")
    )
    return Response({"detail": "Tracking event accepted."})
