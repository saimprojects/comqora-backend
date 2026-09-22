from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from rest_framework.response import Response
from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    if isinstance(exc, DjangoValidationError):
        return Response({"detail": exc.messages}, status=400)
    if isinstance(exc, IntegrityError):
        return Response(
            {"detail": "This change conflicts with an existing record. Refresh and try again."},
            status=409,
        )
    return exception_handler(exc, context)
