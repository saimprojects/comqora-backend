from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import cloudinary
import cloudinary.uploader
from PIL import Image, UnidentifiedImageError
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.api import audit

from .api import ManageWhatsApp
from .models import WhatsAppMedia


@api_view(["POST"])
@permission_classes([ManageWhatsApp])
def upload(request):
    file = request.FILES.get("file")
    if not file or not 0 < file.size <= 10 * 1024 * 1024:
        raise ValidationError("Choose a JPEG, PNG, PDF or MP4 file up to 10 MB.")
    suffix = Path(file.name).suffix.lower()
    types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".pdf": "application/pdf",
        ".mp4": "video/mp4",
    }
    if suffix not in types:
        raise ValidationError("Supported attachments: JPEG, PNG, PDF and MP4.")
    mimetype = types[suffix]
    try:
        if mimetype.startswith("image/"):
            img = Image.open(file)
            if (
                img.format != ("PNG" if suffix == ".png" else "JPEG")
                or img.width * img.height > 25_000_000
            ):
                raise ValueError()
            img.verify()
        else:
            header = file.read(12)
            if (suffix == ".pdf" and not header.startswith(b"%PDF-")) or (
                suffix == ".mp4" and header[4:8] != b"ftyp"
            ):
                raise ValueError()
        file.seek(0)
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise ValidationError("The file content does not match its supported format.") from None
    if not cloudinary.config().api_secret:
        return Response({"detail": "Configure Cloudinary to attach media."}, status=503)
    resource = "image" if mimetype.startswith("image/") else "video" if suffix == ".mp4" else "raw"
    # New immutable asset per upload; never accept a tenant-provided remote URL.
    public_id = f"sellflow/{request.user.workspace_id}/whatsapp/{uuid4().hex}"
    if resource == "raw":
        public_id += suffix
    try:
        result = cloudinary.uploader.upload(
            file, public_id=public_id, resource_type=resource, overwrite=False
        )
    except cloudinary.exceptions.Error:
        return Response(
            {"detail": "Media upload failed. Check Cloudinary configuration."}, status=502
        )
    url = result.get("secure_url", "")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "res.cloudinary.com"
        or parsed.username
        or parsed.password
    ):
        return Response({"detail": "Upload returned an unsupported media URL."}, status=502)
    media = WhatsAppMedia.objects.create(
        workspace=request.user.workspace,
        url=url,
        filename="attachment" + suffix,
        mimetype=mimetype,
        size=file.size,
    )
    audit(request, "whatsapp.media_uploaded", media)
    return Response(
        {
            "id": str(media.pk),
            "url": media.url,
            "filename": media.filename,
            "mimetype": media.mimetype,
        },
        status=201,
    )
