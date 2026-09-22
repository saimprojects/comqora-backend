from rest_framework import permissions, viewsets
from rest_framework.exceptions import PermissionDenied

from .models import AuditEvent


class WorkspacePermission(permissions.BasePermission):
    def has_permission(self, request, view):
        user = request.user
        if not user.is_authenticated or not user.workspace_id or not user.has_dashboard_access:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        allowed = getattr(view, "write_roles", ["owner", "manager"])
        return user.role in allowed


def audit(request, action, obj=None, detail=None):
    AuditEvent.objects.create(
        workspace=request.user.workspace,
        actor=request.user,
        action=action,
        object_id=str(obj.pk) if obj else "",
        detail=detail or {},
    )


class TenantViewSet(viewsets.ModelViewSet):
    permission_classes = [WorkspacePermission]

    def get_queryset(self):
        return super().get_queryset().filter(workspace_id=self.request.user.workspace_id)

    def perform_create(self, serializer):
        obj = serializer.save(workspace=self.request.user.workspace)
        audit(self.request, f"{obj._meta.model_name}.created", obj)

    def perform_update(self, serializer):
        obj = serializer.save()
        audit(self.request, f"{obj._meta.model_name}.updated", obj)

    def perform_destroy(self, instance):
        raise PermissionDenied(
            "Records are retained for financial traceability. Archive products or deactivate couriers instead."
        )
