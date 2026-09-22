from decimal import ROUND_DOWN, Decimal

from django.db import transaction
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.api import TenantViewSet, audit
from apps.core.models import Workspace
from apps.orders.models import Order

from .models import AdAllocation, Campaign


class CampaignSerializer(serializers.ModelSerializer):
    allocation_count = serializers.SerializerMethodField()

    def get_allocation_count(self, obj):
        return sum(1 for a in obj.allocations.all() if a.active)

    class Meta:
        model = Campaign
        exclude = ["workspace"]
        read_only_fields = ["id", "created_at", "updated_at", "allocated"]

    def validate(self, attrs):
        if attrs["end_date"] < attrs["start_date"]:
            raise serializers.ValidationError("End date must be on or after start date.")
        return attrs


class CampaignViewSet(TenantViewSet):
    queryset = Campaign.objects.prefetch_related("allocations")
    serializer_class = CampaignSerializer
    http_method_names = ["get", "post", "head", "options"]
    search_fields = ["name", "channel"]

    def get_queryset(self):
        qs = super().get_queryset()
        start = self.request.query_params.get("start_date")
        end = self.request.query_params.get("end_date")
        if start:
            qs = qs.filter(end_date__gte=serializers.DateField().run_validation(start))
        if end:
            qs = qs.filter(start_date__lte=serializers.DateField().run_validation(end))
        return qs

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def allocate(self, request, pk=None):
        self.get_object()
        Workspace.objects.select_for_update().get(pk=request.user.workspace_id)
        campaign = Campaign.objects.select_for_update().get(pk=pk, workspace=request.user.workspace)
        if campaign.allocated:
            return Response({"detail": "Already allocated. Undo before reallocating."}, status=400)
        mode = request.data.get("mode", "skip_existing")
        if mode not in ["skip_existing", "add"]:
            return Response({"detail": "Choose skip_existing or add mode."}, status=400)
        orders = (
            Order.objects.select_for_update()
            .filter(
                workspace=request.user.workspace,
                created_at__date__gte=campaign.start_date,
                created_at__date__lte=campaign.end_date,
            )
            .order_by("pk")
        )
        if mode == "skip_existing":
            orders = orders.filter(ad_cost=0)
        orders = list(orders)
        if not orders:
            return Response({"detail": "No eligible orders in this date range."}, status=400)
        amount = (campaign.spend / len(orders)).quantize(Decimal(".01"), rounding=ROUND_DOWN)
        for index, order in enumerate(orders):
            share = (
                campaign.spend - amount * (len(orders) - 1) if index == len(orders) - 1 else amount
            )
            AdAllocation.objects.create(
                workspace=request.user.workspace, campaign=campaign, order=order, amount=share
            )
            order.ad_cost += share
            order.save(update_fields=["ad_cost"])
        campaign.allocated = True
        campaign.save(update_fields=["allocated"])
        audit(request, "campaign.allocated", campaign, {"mode": mode, "orders": len(orders)})
        return Response(CampaignSerializer(campaign).data)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def undo(self, request, pk=None):
        self.get_object()
        Workspace.objects.select_for_update().get(pk=request.user.workspace_id)
        campaign = Campaign.objects.select_for_update().get(pk=pk, workspace=request.user.workspace)
        for allocation in campaign.allocations.filter(active=True).order_by("order_id"):
            order = Order.objects.select_for_update().get(pk=allocation.order_id)
            order.ad_cost -= allocation.amount
            order.save(update_fields=["ad_cost"])
            allocation.active = False
            allocation.save(update_fields=["active"])
        campaign.allocated = False
        campaign.save(update_fields=["allocated"])
        audit(request, "campaign.allocation_undone", campaign)
        return Response(CampaignSerializer(campaign).data)
