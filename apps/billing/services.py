import calendar

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.core.models import AuditEvent, Workspace

from .models import Payment, Subscription


def next_month(value):
    year, month = (value.year + 1, 1) if value.month == 12 else (value.year, value.month + 1)
    return value.replace(
        year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1])
    )


@transaction.atomic
def review_payment(payment_id, reviewer, approve):
    if not reviewer.is_active or not reviewer.is_superuser:
        raise ValidationError("Only platform administrators can review payments.")
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    if payment.status != "PENDING":
        raise ValidationError("Only payments pending review can be approved or rejected.")
    if not payment.proof:
        raise ValidationError("Payment proof is required.")
    if not approve and not payment.review_note.strip():
        raise ValidationError("Save a review note explaining the rejection first.")
    Workspace.objects.select_for_update().get(pk=payment.workspace_id)
    now = timezone.now()
    if approve:
        subscription = Subscription.objects.filter(workspace=payment.workspace).first()
        start = now
        if subscription and subscription.is_active and subscription.plan_id == payment.plan_id:
            start = subscription.expires_at
        Subscription.objects.update_or_create(
            workspace=payment.workspace,
            defaults={
                "plan": payment.plan,
                "starts_at": subscription.starts_at if start != now else now,
                "expires_at": next_month(start),
                "suspended": False,
            },
        )
        User.objects.filter(
            workspace=payment.workspace, dashboard_access_state=User.DASHBOARD_PENDING
        ).update(
            dashboard_access_state=User.DASHBOARD_ACTIVE,
            dashboard_unlocked_at=now,
            dashboard_unlocked_by=reviewer,
        )
    payment.status = "APPROVED" if approve else "REJECTED"
    payment.reviewed_by = reviewer
    payment.reviewed_at = now
    payment.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    AuditEvent.objects.create(
        workspace=payment.workspace,
        actor=reviewer,
        action=f"Billing.{payment.status}",
        object_id=str(payment.pk),
        detail={"amount": str(payment.amount), "plan": payment.plan_name},
    )
    return payment
