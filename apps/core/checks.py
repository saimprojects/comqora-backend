from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import Error, register


@register("comqora", deploy=True)
def production_config(app_configs, **kwargs):
    if settings.DEBUG:
        return []  # Django's own deployment checks reject DEBUG=True.
    errors = []
    frontend = urlsplit(settings.FRONTEND_URL)
    if (
        frontend.scheme != "https"
        or not frontend.netloc
        or frontend.path not in {"", "/"}
        or frontend.query
        or frontend.fragment
        or frontend.username
    ):
        errors.append(
            Error(
                "FRONTEND_URL must be an HTTPS origin without a path or credentials.",
                id="comqora.E001",
            )
        )
    if "*" in settings.ALLOWED_HOSTS:
        errors.append(
            Error("Use explicit production ALLOWED_HOSTS instead of '*'.", id="comqora.E002")
        )
    if settings.REQUIRE_EMAIL_VERIFICATION:
        backend = settings.EMAIL_BACKEND
        if backend in {
            "django.core.mail.backends.console.EmailBackend",
            "django.core.mail.backends.locmem.EmailBackend",
            "django.core.mail.backends.dummy.EmailBackend",
            "django.core.mail.backends.filebased.EmailBackend",
        }:
            errors.append(
                Error(
                    "Configure a delivering email backend before requiring email verification.",
                    id="comqora.E003",
                )
            )
        elif backend == "django.core.mail.backends.smtp.EmailBackend" and not settings.EMAIL_HOST:
            errors.append(
                Error(
                    "Set EMAIL_HOST for production verification and recovery email.",
                    id="comqora.E004",
                )
            )
    return errors
