"""Database-backed queue, consumed independently of web requests. No billable auto-retry."""

import logging
from datetime import timedelta
from uuid import uuid4

from django.db import close_old_connections
from django.utils import timezone

from .models import Turn

ACTIVE_STATUSES = ("QUEUED", "RUNNING")


def expire_interrupted(queryset):
    # Inactivity lease, NOT total research duration. Each model call is bounded below this.
    return queryset.filter(
        status="RUNNING", updated_at__lt=timezone.now() - timedelta(minutes=15)
    ).update(
        status="ERROR",
        error="Research worker was interrupted. Send a new message to retry.",
        finished_at=timezone.now(),
        processing_token=None,
    )


def process_turns():
    from .service import run

    close_old_connections()
    try:
        expire_interrupted(Turn.objects.all())
        pk = (
            Turn.objects.filter(status="QUEUED")
            .order_by("created_at", "id")
            .values_list("pk", flat=True)
            .first()
        )
        if not pk:
            return 0
        token = uuid4()
        if not Turn.objects.filter(pk=pk, status="QUEUED").update(
            status="RUNNING", processing_token=token, updated_at=timezone.now()
        ):
            return 0
        try:
            turn = Turn.objects.select_related(
                "workspace", "model__connection", "conversation__user"
            ).get(pk=pk)
            run(turn)
        except Exception as exc:
            logging.getLogger(__name__).warning("Research worker failed (%s).", type(exc).__name__)
            Turn.objects.filter(pk=pk, status="RUNNING", processing_token=token).update(
                status="ERROR",
                error="Research worker could not complete the request.",
                finished_at=timezone.now(),
                processing_token=None,
            )
        return 1
    finally:
        close_old_connections()


def research_loop(stop):
    while not stop.is_set():
        try:
            processed = process_turns()
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Research queue pass failed (%s).", type(exc).__name__
            )
            processed = 0
        if not processed:
            stop.wait(2)
