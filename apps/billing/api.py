import io
import warnings

from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from PIL import Image, UnidentifiedImageError
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.core.models import Workspace

from .models import Payment, PaymentBank, Plan, Subscription


def owner(request):
    if not request.user.workspace_id or request.user.role != "owner":
        raise PermissionDenied("Only the workspace owner can manage billing.")


def payment_data(payment):
    return {
        key: getattr(payment, key)
        for key in [
            "id",
            "plan_name",
            "amount",
            "bank_details",
            "status",
            "reference",
            "created_at",
            "submitted_at",
            "reviewed_at",
            "review_note",
        ]
    }


def subscription_data(user):
    sub = Subscription.objects.select_related("plan").filter(workspace_id=user.workspace_id).first()
    return (
        None
        if not sub
        else {
            "plan": sub.plan.name,
            "ai_enabled": sub.is_active and sub.plan.ai_enabled,
            "active": sub.is_active,
            "expires_at": sub.expires_at,
            "suspended": sub.suspended,
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def plans(request):
    return Response(
        list(
            Plan.objects.filter(active=True).values(
                "id", "name", "slug", "monthly_price", "ai_enabled"
            )
        )
    )


@api_view(["GET", "POST"])
def checkout(request):
    owner(request)
    if request.method == "GET":
        return Response(
            {
                "banks": [
                    {
                        **{
                            key: getattr(bank, key)
                            for key in [
                                "id",
                                "bank_name",
                                "account_title",
                                "account_number",
                                "iban",
                                "instructions",
                            ]
                        },
                        "icon_url": request.build_absolute_uri(
                            reverse("billing-bank-icon", args=[bank.pk])
                        )
                        if bank.icon
                        else None,
                    }
                    for bank in PaymentBank.objects.filter(active=True)
                ],
                "subscription": subscription_data(request.user),
                "payments": [
                    payment_data(p)
                    for p in Payment.objects.filter(workspace=request.user.workspace).defer(
                        "proof"
                    )[:50]
                ],
            }
        )

    class CheckoutInput(serializers.Serializer):
        plan = serializers.PrimaryKeyRelatedField(queryset=Plan.objects.filter(active=True))
        bank = serializers.PrimaryKeyRelatedField(queryset=PaymentBank.objects.filter(active=True))

    data = CheckoutInput(data=request.data)
    data.is_valid(raise_exception=True)
    with transaction.atomic():
        Workspace.objects.select_for_update().get(pk=request.user.workspace_id)
        existing = Payment.objects.filter(
            workspace=request.user.workspace, status__in=["AWAITING_PROOF", "PENDING"]
        ).first()
        if existing:
            return Response(
                {"detail": "Complete or cancel your existing checkout first."}, status=409
            )
        plan, bank = data.validated_data["plan"], data.validated_data["bank"]
        payment = Payment.objects.create(
            workspace=request.user.workspace,
            submitted_by=request.user,
            plan=plan,
            plan_name=plan.name,
            amount=plan.monthly_price,
            bank=bank,
            bank_details={
                key: getattr(bank, key)
                for key in ["bank_name", "account_title", "account_number", "iban", "instructions"]
            },
        )
    return Response(payment_data(payment), status=201)


@api_view(["GET"])
@permission_classes([AllowAny])
def bank_icon(request, pk):
    bank = get_object_or_404(PaymentBank, pk=pk, icon__isnull=False)
    response = HttpResponse(bytes(bank.icon), content_type="image/png")
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


class ProofInput(serializers.Serializer):
    proof = serializers.FileField()
    reference = serializers.CharField(max_length=120)

    def validate_proof(self, upload):
        if upload.size > 5 * 1024 * 1024:
            raise serializers.ValidationError("Screenshot must be 5 MB or smaller.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                image = Image.open(upload)
                if (
                    image.format not in ["PNG", "JPEG", "WEBP"]
                    or image.width * image.height > 20_000_000
                ):
                    raise ValueError()
                image.load()
                output = io.BytesIO()
                image.convert("RGB").save(output, format="JPEG", quality=90)
                content = output.getvalue()
                if len(content) > 5 * 1024 * 1024:
                    raise ValueError()
                return content
        except (
            OSError,
            ValueError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            raise serializers.ValidationError(
                "Upload a valid PNG, JPEG or WebP screenshot up to 20 megapixels."
            )


@api_view(["POST"])
def submit_proof(request, pk):
    owner(request)
    data = ProofInput(data=request.data)
    data.is_valid(raise_exception=True)
    with transaction.atomic():
        payment = get_object_or_404(
            Payment.objects.select_for_update(), pk=pk, workspace=request.user.workspace
        )
        if payment.status != "AWAITING_PROOF":
            return Response({"detail": "This checkout is already submitted or closed."}, status=409)
        payment.proof = data.validated_data["proof"]
        payment.proof_type = "image/jpeg"
        payment.reference = data.validated_data["reference"]
        payment.status = "PENDING"
        payment.submitted_at = timezone.now()
        payment.save(update_fields=["proof", "proof_type", "reference", "status", "submitted_at"])
    return Response(payment_data(payment))


@api_view(["POST"])
def cancel(request, pk):
    owner(request)
    updated = Payment.objects.filter(
        pk=pk, workspace=request.user.workspace, status="AWAITING_PROOF"
    ).update(status="CANCELLED")
    return Response(
        {
            "detail": "Checkout cancelled."
            if updated
            else "Only an unsubmitted checkout can be cancelled."
        },
        status=200 if updated else 409,
    )


@api_view(["GET"])
def proof(request, pk):
    queryset = Payment.objects.all()
    if not request.user.is_superuser:
        owner(request)
        queryset = queryset.filter(workspace=request.user.workspace)
    payment = get_object_or_404(queryset, pk=pk, proof__isnull=False)
    response = HttpResponse(bytes(payment.proof), content_type="image/jpeg")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
