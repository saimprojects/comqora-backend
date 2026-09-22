from io import BytesIO

import cloudinary
from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.response import Response

from apps.accounts.access import locked_payload
from apps.logistics.tracking import worker_health

from .api import audit

INVOICE_TEMPLATES = ("studio", "editorial", "ledger", "noir", "sage", "royal", "rose", "compact")


class BrandSettings(serializers.Serializer):
    name = serializers.CharField(max_length=120, required=False)
    business_address = serializers.CharField(max_length=500, allow_blank=True, required=False)
    business_phone = serializers.CharField(max_length=40, allow_blank=True, required=False)
    business_email = serializers.EmailField(allow_blank=True, required=False)
    invoice_template = serializers.ChoiceField(choices=INVOICE_TEMPLATES, required=False)
    invoice_footer = serializers.CharField(max_length=300, allow_blank=True, required=False)


@api_view(["GET", "POST", "DELETE"])
def workspace_logo(request):
    if not request.user.has_dashboard_access:
        return Response(locked_payload(request.user), status=423)
    if not request.user.workspace_id:
        return Response({"detail": "Business workspace required."}, status=403)
    ws = request.user.workspace
    if request.method == "GET":
        if not ws.logo:
            return Response({"detail": "No brand logo uploaded."}, status=404)
        response = HttpResponse(bytes(ws.logo), content_type="image/png")
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
    if request.user.role != "owner":
        return Response({"detail": "Only the owner can update the brand logo."}, status=403)
    if request.method == "DELETE":
        ws.logo = None
    else:
        upload = request.FILES.get("logo")
        if not upload or upload.size > 2 * 1024 * 1024:
            return Response({"detail": "Upload a PNG, JPEG or WebP logo up to 2 MB."}, status=400)
        try:
            raw = upload.read(2 * 1024 * 1024 + 1)
            with Image.open(BytesIO(raw)) as image:
                if (
                    image.format not in {"PNG", "JPEG", "WEBP"}
                    or image.width * image.height > 10_000_000
                ):
                    raise ValueError("Unsupported image")
                image.verify()
            with Image.open(BytesIO(raw)) as image:
                clean = ImageOps.exif_transpose(image).convert("RGBA")
                clean.thumbnail((1024, 1024))
                output = BytesIO()
                clean.save(output, format="PNG", optimize=True)
                ws.logo = output.getvalue()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            return Response(
                {"detail": "This logo could not be read. Choose a valid PNG, JPEG or WebP image."},
                status=400,
            )
    ws.logo_updated_at = timezone.now()
    ws.save(update_fields=["logo", "logo_updated_at"])
    audit(request, "workspace.logo_updated", ws)
    return Response({"has_logo": bool(ws.logo), "logo_updated_at": ws.logo_updated_at})


@api_view(["GET", "PATCH"])
def workspace(request):
    if not request.user.has_dashboard_access:
        return Response(locked_payload(request.user), status=423)
    if not request.user.workspace_id:
        return Response({"detail": "Business workspace required."}, status=403)
    ws = request.user.workspace
    if request.method == "PATCH":
        if request.user.role != "owner":
            return Response({"detail": "Only the owner can update workspace settings."}, status=403)
        serializer = BrandSettings(data=request.data)
        serializer.is_valid(raise_exception=True)
        for key, value in serializer.validated_data.items():
            setattr(ws, key, value)
        if serializer.validated_data:
            ws.save(update_fields=list(serializer.validated_data))
        audit(request, "workspace.updated", ws)
    return Response(
        {
            "id": ws.pk,
            "name": ws.name,
            "currency": ws.currency,
            "has_logo": bool(ws.logo),
            "logo_updated_at": ws.logo_updated_at,
            "business_address": ws.business_address,
            "business_phone": ws.business_phone,
            "business_email": ws.business_email,
            "invoice_template": ws.invoice_template,
            "invoice_footer": ws.invoice_footer,
            "integrations": {
                "cloudinary": bool(
                    cloudinary.config().api_secret and cloudinary.config().cloud_name
                ),
                "tracking": settings.TRACKING_ENABLED,
                "tracking_worker": worker_health(),
                "postex": bool(settings.POSTEX_API_TOKEN),
                "email": settings.EMAIL_BACKEND
                not in [
                    "django.core.mail.backends.console.EmailBackend",
                    "django.core.mail.backends.locmem.EmailBackend",
                ],
            },
        }
    )
