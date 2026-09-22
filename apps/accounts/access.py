"""Shared server-side dashboard access guardrails."""

SUPPORT_CONTACT = "+923131471263"
LOCKED_DETAIL = f"Your dashboard is locked. Sign in and choose a plan at /billing, then submit payment proof for approval. Support: {SUPPORT_CONTACT}."


def dashboard_is_locked(user):
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "workspace_id", None)
        and not getattr(user, "has_dashboard_access", False)
    )


def locked_payload(user=None):
    return {
        "code": "dashboard_locked",
        "detail": LOCKED_DETAIL,
        "dashboard_access_state": getattr(user, "dashboard_access_state", "PENDING"),
        "support_contact": SUPPORT_CONTACT,
    }
