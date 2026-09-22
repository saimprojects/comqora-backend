"""Deny locked workspace sessions before any data endpoint can run."""

from django.http import JsonResponse

from .access import dashboard_is_locked, locked_payload


class DashboardLockMiddleware:
    """A defence-in-depth guard for views without workspace permissions."""

    exempt_prefixes = (
        "/api/auth/",
        "/api/billing/",
        "/api/public/",
        "/api/health/",
        "/api/integrations/tracking/webhook/",
        "/api/whatsapp/webhook/",
        "/api/whatsapp/unsubscribe/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.path.startswith("/api/")
            and not request.path.startswith(self.exempt_prefixes)
            and dashboard_is_locked(request.user)
        ):
            return JsonResponse(locked_payload(request.user), status=423)
        return self.get_response(request)
