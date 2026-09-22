from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower


class User(AbstractUser):
    DASHBOARD_PENDING = "PENDING"
    DASHBOARD_ACTIVE = "ACTIVE"
    DASHBOARD_SUSPENDED = "SUSPENDED"
    DASHBOARD_ACCESS_CHOICES = [
        (DASHBOARD_PENDING, "Pending Jazzmin approval"),
        (DASHBOARD_ACTIVE, "Dashboard unlocked"),
        (DASHBOARD_SUSPENDED, "Dashboard suspended"),
    ]

    email = models.EmailField(unique=True)
    workspace = models.ForeignKey("core.Workspace", null=True, blank=True, on_delete=models.PROTECT)
    role = models.CharField(
        max_length=12,
        choices=[(x, x.title()) for x in ["owner", "manager", "staff", "viewer"]],
        default="owner",
    )
    email_verified = models.BooleanField(default=False)
    dashboard_access_state = models.CharField(
        max_length=12, choices=DASHBOARD_ACCESS_CHOICES, default=DASHBOARD_PENDING
    )
    dashboard_unlocked_at = models.DateTimeField(null=True, blank=True)
    dashboard_unlocked_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="unlocked_dashboard_users",
    )

    @property
    def has_dashboard_access(self):
        from apps.billing.models import Subscription

        if self.dashboard_access_state != self.DASHBOARD_ACTIVE or not self.workspace_id:
            return False
        sub = Subscription.objects.filter(workspace_id=self.workspace_id).first()
        return bool(sub and sub.is_active)

    @property
    def has_ai_access(self):
        from apps.billing.models import Subscription

        if not self.has_dashboard_access:
            return False
        return Subscription.objects.filter(
            workspace_id=self.workspace_id, plan__ai_enabled=True
        ).exists()

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), name="unique_user_email_ci")]
