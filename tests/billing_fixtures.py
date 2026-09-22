from datetime import timedelta

from django.utils import timezone

from apps.billing.models import Plan, Subscription
from apps.core.models import Workspace


def paid_workspace(**kwargs):
    """Business/AI tests operate inside an explicitly subscribed workspace."""
    workspace = Workspace.objects.create(**kwargs)
    plan, _ = Plan.objects.get_or_create(
        slug="ultra-ai",
        defaults={"name": "Ultra AI", "monthly_price": "4599.00", "ai_enabled": True},
    )
    Subscription.objects.create(
        workspace=workspace,
        plan=plan,
        starts_at=timezone.now() - timedelta(days=1),
        expires_at=timezone.now() + timedelta(days=30),
    )
    return workspace
