from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils import timezone

from apps.core.models import AuditEvent

from .models import User


@admin.register(User)
class SellFlowUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("Comqora access", {"fields": ["workspace", "role", "email_verified"]}),
        (
            "Dashboard lock",
            {
                "fields": [
                    "dashboard_access_state",
                    "dashboard_unlocked_at",
                    "dashboard_unlocked_by",
                ]
            },
        ),
    )
    list_display = [
        "email",
        "first_name",
        "workspace",
        "role",
        "dashboard_access_state",
        "is_active",
        "is_staff",
    ]
    list_filter = ["dashboard_access_state", "role", "is_active", "is_staff"]
    readonly_fields = ["dashboard_unlocked_at", "dashboard_unlocked_by"]
    actions = ["unlock_dashboard", "suspend_dashboard"]

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def _record_access_change(self, request, user, state):
        if user.workspace_id:
            AuditEvent.objects.create(
                workspace=user.workspace,
                actor=request.user,
                action="Dashboard.AccessChanged",
                object_id=str(user.pk),
                detail={"state": state, "via": "jazzmin"},
            )

    @admin.action(description="Unlock selected dashboards")
    def unlock_dashboard(self, request, queryset):
        for user in queryset.exclude(dashboard_access_state=User.DASHBOARD_ACTIVE):
            user.dashboard_access_state = User.DASHBOARD_ACTIVE
            user.dashboard_unlocked_at = timezone.now()
            user.dashboard_unlocked_by = request.user
            user.save(
                update_fields=[
                    "dashboard_access_state",
                    "dashboard_unlocked_at",
                    "dashboard_unlocked_by",
                ]
            )
            self._record_access_change(request, user, User.DASHBOARD_ACTIVE)

    @admin.action(description="Suspend selected dashboards")
    def suspend_dashboard(self, request, queryset):
        for user in queryset.exclude(dashboard_access_state=User.DASHBOARD_SUSPENDED):
            user.dashboard_access_state = User.DASHBOARD_SUSPENDED
            user.save(update_fields=["dashboard_access_state"])
            self._record_access_change(request, user, User.DASHBOARD_SUSPENDED)

    def save_model(self, request, obj, form, change):
        was_active = False
        if change:
            was_active = User.objects.filter(
                pk=obj.pk, dashboard_access_state=User.DASHBOARD_ACTIVE
            ).exists()
        if obj.dashboard_access_state == User.DASHBOARD_ACTIVE and not was_active:
            obj.dashboard_unlocked_at = timezone.now()
            obj.dashboard_unlocked_by = request.user
        super().save_model(request, obj, form, change)
        if change and "dashboard_access_state" in form.changed_data:
            self._record_access_change(request, obj, obj.dashboard_access_state)
