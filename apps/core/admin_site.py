"""The Jazzmin control panel is reserved for platform superusers."""

from django.contrib.admin import AdminSite
from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.exceptions import ValidationError


class SuperuserAdminAuthenticationForm(AdminAuthenticationForm):
    """Reject staff accounts that are not platform administrators at login."""

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_superuser:
            raise ValidationError(
                "This administration area is available only to a Jazzmin superuser.",
                code="admin_superuser_required",
            )


class JazzminSuperuserAdminSite(AdminSite):
    """Do not let a pending or ordinary staff user reach any admin model."""

    login_form = SuperuserAdminAuthenticationForm

    def has_permission(self, request):
        user = request.user
        return bool(
            user.is_authenticated and user.is_active and user.is_staff and user.is_superuser
        )
