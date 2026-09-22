import hashlib
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.orders.models import Order, TrackingEvent
from apps.orders.services import money

from .models import TrackingWorkerState
from .providers import RUN_PROVIDERS, TrackingError, fetch_tracking, normalize_status

logger = logging.getLogger(__name__)
ACTIVE = Order.ACTIVE_SHIPMENT_STATUSES


def worker_health():
    stamp = (
        TrackingWorkerState.objects.filter(name="tracking")
        .values_list("heartbeat", flat=True)
        .first()
    )
    return {
        "last_heartbeat": stamp,
        "running": bool(stamp and stamp > timezone.now() - timedelta(seconds=120)),
    }


def tracking_info(order):
    provider = order.tracking_provider or order.courier.provider
    supported = provider in RUN_PROVIDERS or provider == "PostEx"
    demo = order.tracking_id.startswith("DEMO") and order.workspace.name.endswith(" · Demo")
    configured = (
        settings.TRACKING_ENABLED
        and supported
        and not demo
        and (provider != "PostEx" or bool(settings.POSTEX_API_TOKEN))
    )
    return {
        "provider": provider,
        "mode": order.tracking_mode,
        "source": "postex" if provider == "PostEx" else "run_courier" if supported else "none",
        "supported": supported,
        "configured": configured,
        "poll_seconds": settings.TRACKING_POLL_SECONDS,
        "warning": "Manual updates enabled — automatic polling is paused."
        if order.tracking_mode == "MANUAL"
        else "Demo shipment — automatic tracking is disabled."
        if demo
        else "Auto Tracking not available for other shipping services."
        if not supported
        else ""
        if configured
        else "Automatic tracking is disabled or the server PostEx token is not configured.",
    }


@transaction.atomic
def apply_checkpoints(order_id, source, checkpoints, lease_token):
    order = Order.objects.select_for_update().get(pk=order_id)
    previous_status = order.status
    if order.tracking_mode != "AUTO" or order.tracking_lock_token != lease_token:
        return False
    now = timezone.now()
    candidates = []
    for point in checkpoints:
        status = normalize_status(point.text)
        fingerprint = "|".join(
            [
                str(order.pk),
                source,
                order.tracking_id,
                point.text,
                point.occurred_at.isoformat() if point.occurred_at else "",
                point.code,
            ]
        )
        event_id = source + ":" + hashlib.sha256(fingerprint.encode()).hexdigest()
        event, _ = TrackingEvent.objects.get_or_create(
            provider_event_id=event_id,
            defaults={
                "order": order,
                "workspace_id": order.workspace_id,
                "status": status or "UNKNOWN",
                "message": point.text[:250],
                "raw_status": point.text,
                "occurred_at": point.occurred_at,
                "source": source,
            },
        )
        # Re-normalize existing history after mapping upgrades without duplicating events.
        if event.status != (status or "UNKNOWN"):
            event.status = status or "UNKNOWN"
            event.save(update_fields=["status"])
        # Undated summaries retain their first observation time on replay.
        candidates.append((status, event.occurred_at or event.created_at))
    candidate = sorted(candidates, key=lambda item: item[1])[-1] if candidates else None
    # Historical checkpoints can be imported after finalization but never regress finances.
    if candidate and candidate[0] and order.status in ACTIVE:
        status, stamp = candidate
        if not order.tracking_status_at or stamp >= order.tracking_status_at:
            order.status = status
            order.tracking_status_at = stamp
            if status == "DELIVERED":
                order.finalized_at = stamp
            elif status == "RETURNED":
                order.return_cost = money(order.courier_snapshot["return_rate"])
    order.tracking_checked_at = now
    order.tracking_error = (
        "Unmapped courier status received; history saved, review shipment outcome."
        if candidate and candidate[0] is None
        else ""
    )
    order.tracking_failures = 0
    order.tracking_next_sync_at = (
        now + timedelta(seconds=settings.TRACKING_POLL_SECONDS) if order.status in ACTIVE else None
    )
    order.tracking_lock_until = None
    order.tracking_lock_token = None
    order.save()
    if order.status != previous_status:
        from apps.messaging.services import enqueue_order

        enqueue_order(order, order.status, order.tracking_status_at.isoformat())
    return True


def sync_order(order_id, *, workspace_id=None):
    """One short DB claim, HTTP outside transactions, one atomic history update."""
    now = timezone.now()
    qs = Order.objects.filter(pk=order_id)
    if workspace_id is not None:
        qs = qs.filter(workspace_id=workspace_id)
    order = qs.select_related("courier").first()
    if not order:
        raise TrackingError("Order not found.")
    info = tracking_info(order)
    if order.tracking_mode != "AUTO":
        return {
            "state": "idle",
            "detail": "Manual mode is enabled. Resume automatic tracking first.",
        }
    if not info["configured"]:
        if settings.TRACKING_ENABLED and info["supported"]:
            qs.update(
                tracking_error=info["warning"], tracking_next_sync_at=now + timedelta(minutes=5)
            )
        raise TrackingError(info["warning"])
    if not order.tracking_id or order.status not in ACTIVE:
        return {"state": "idle", "detail": "Only dispatched, unresolved shipments are polled."}
    token = uuid.uuid4()
    claimed = (
        qs.filter(status__in=ACTIVE, tracking_mode="AUTO")
        .filter(Q(tracking_next_sync_at__isnull=True) | Q(tracking_next_sync_at__lte=now))
        .filter(Q(tracking_lock_until__isnull=True) | Q(tracking_lock_until__lte=now))
        .update(
            tracking_lock_token=token,
            tracking_lock_until=now + timedelta(seconds=90),
            tracking_attempted_at=now,
        )
    )
    if not claimed:
        return {
            "state": "waiting",
            "detail": "Tracking is already refreshing or the next check is not due yet.",
        }
    try:
        source, checkpoints = fetch_tracking(
            info["provider"], order.tracking_id, order.workspace_id
        )
        applied = apply_checkpoints(order.pk, source, checkpoints, token)
        return {"state": "updated" if applied else "waiting", "detail": "Courier tracking checked."}
    except Exception as exc:
        safe = (
            str(exc)
            if isinstance(exc, TrackingError)
            else "Tracking processing failed. Automatic retry is scheduled."
        )
        if not isinstance(exc, TrackingError):
            logger.error(
                "Tracking processing failed for order %s (%s)", order.pk, type(exc).__name__
            )
        failures = order.tracking_failures + 1
        delay = min(3600, settings.TRACKING_POLL_SECONDS * 2 ** min(failures, 6))
        qs.filter(tracking_lock_token=token).update(
            tracking_failures=failures,
            tracking_error=safe[:250],
            tracking_next_sync_at=timezone.now() + timedelta(seconds=delay),
            tracking_lock_until=None,
            tracking_lock_token=None,
        )
        raise TrackingError(safe) from None


def sync_due(limit=100):
    TrackingWorkerState.objects.update_or_create(
        name="tracking", defaults={"heartbeat": timezone.now()}
    )
    if not settings.TRACKING_ENABLED:
        return 0
    now = timezone.now()
    ids = (
        Order.objects.filter(status__in=ACTIVE, tracking_mode="AUTO")
        .exclude(tracking_id="")
        .exclude(tracking_provider="Others")
        .filter(Q(tracking_next_sync_at__isnull=True) | Q(tracking_next_sync_at__lte=now))
        .order_by("tracking_next_sync_at", "created_at")
        .values_list("pk", flat=True)[:limit]
    )
    count = 0
    for pk in list(ids):
        TrackingWorkerState.objects.update_or_create(
            name="tracking", defaults={"heartbeat": timezone.now()}
        )
        try:
            sync_order(pk)
            count += 1
        except TrackingError:
            pass  # The order carries a sanitized error and a retry schedule.
    return count
