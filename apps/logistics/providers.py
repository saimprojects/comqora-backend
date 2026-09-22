"""Read-only courier tracking clients. Credentials never leave the PostEx host."""

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import urllib3
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

RUN_BASE = "https://portal.runcourier.com/API/"
POSTEX_BASE = "https://api.postex.pk/services/integration/api/order/v1/track-order/"
RUN_PROVIDERS = {"TCS", "Leopards", "M&P", "Trax", "Daewoo", "Dastaq Logistic", "AHL"}
http = urllib3.PoolManager()


class TrackingError(Exception):
    """Safe, user-facing error: no token, URL or provider customer data."""


@dataclass(frozen=True)
class Checkpoint:
    text: str
    occurred_at: object = None
    code: str = ""


def request_json(method, url, *, payload=None, token=None):
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["token"] = token
    response = None
    try:
        response = http.request(
            method,
            url,
            headers=headers,
            body=json.dumps(payload).encode() if payload is not None else None,
            timeout=urllib3.Timeout(connect=3, read=10),
            retries=False,
            redirect=False,
            preload_content=False,
        )
        if response.status != 200:
            raise TrackingError(
                f"Courier returned HTTP {response.status}. Check service access or credentials."
            )
        body = response.read(1_048_577)
        if len(body) > 1_048_576:
            raise TrackingError("Courier response is too large.")
        return json.loads(body)
    except (urllib3.exceptions.HTTPError, OSError):
        raise TrackingError(
            "Courier connection failed or timed out. Automatic retry is scheduled."
        ) from None
    except (ValueError, UnicodeError):
        raise TrackingError("Courier returned an invalid JSON response.") from None
    finally:
        if response:
            response.close()
            response.release_conn()


def source_time(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise TrackingError("Courier supplied an invalid event timestamp.")
    try:
        stamp = parse_datetime(value)
    except ValueError:
        stamp = None
    if stamp is None:
        raise TrackingError("Courier supplied an invalid event timestamp.")
    if timezone.is_naive(stamp):
        stamp = timezone.make_aware(stamp, ZoneInfo("Asia/Karachi"))
    if stamp > timezone.now() + timedelta(minutes=5):
        raise TrackingError("Courier supplied a future event timestamp; review before applying.")
    return stamp


def checkpoint(text, timestamp=None, code=""):
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise TrackingError("Courier response has no valid status text.")
    return Checkpoint(text, source_time(timestamp), str(code or "")[:80])


def normalize_status(text):
    """Conservative allowlist: 'undelivered'/'return in process' are NOT final."""
    value = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    if value in {
        "delivered",
        "shipment delivered",
        "delivered to consignee",
        "delivered to customer",
    }:
        return "DELIVERED"
    if value in {
        "returned",
        "returned to shipper",
        "returned to sender",
        "return delivered",
        "return completed",
    }:
        return "RETURNED"
    if value in {
        "delivery unsuccessful",
        "delivery failed",
        "delivery attempt failed",
        "refused to accept",
        "refused to receive",
        "attempt made rfd refused to receive",
        "undelivered",
        "not delivered",
        "customer unavailable",
        "consignee not available",
        "delivery under review",
    }:
        return "DELIVERY_FAILED"
    if value in {"out for delivery", "enroute for delivery", "en route for delivery"}:
        return "OUT_FOR_DELIVERY"
    if value in {
        "return in process",
        "return in transit",
        "return initiated",
        "parcel return to office",
        "return received at origin",
        "returned to origin city",
        "return confirmation",
    }:
        return "RETURN_IN_TRANSIT"
    transit = {
        "new booked",
        "booked",
        "picked up",
        "pick up in progress",
        "waiting for delivery",
        "parcel received at destination",
        "parcel in transit to destination",
        "parcel received at office",
        "re attempt",
        "in transit",
        "hold for self collection",
        "shipper advice",
        "under verification",
    }
    if (
        value in transit
        or value.startswith(
            (
                "shipment picked in ",
                "dispatched to ",
                "arrived at ",
                "received at ",
                "departed to ",
                "en route to ",
                "enroute to ",
            )
        )
        or (value.startswith("at ") and value.endswith(" warehouse"))
    ):
        return "IN_TRANSIT"
    return None


def parse_run(data, tracking_id):
    if isinstance(data, dict) and (data.get("error") or "status" not in data):
        raise TrackingError("Run Courier did not return a tracking history for this number.")
    rows = data if isinstance(data, list) else [data]
    if not rows or len(rows) > 2000:
        raise TrackingError("Run Courier has no tracking checkpoints for this number yet.")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise TrackingError("Run Courier response format is not supported.")
        if str(row.get("tracking_no", tracking_id)).strip() != tracking_id:
            raise TrackingError("Courier response belongs to a different tracking number.")
        result.append(checkpoint(row.get("status"), row.get("created")))
    return result


def parse_postex(data, tracking_id):
    if not isinstance(data, dict) or str(data.get("statusCode")) != "200":
        raise TrackingError("PostEx could not find an accessible order for this tracking number.")
    order = data.get("dist")
    if not isinstance(order, dict) or str(order.get("trackingNumber", "")).strip() != tracking_id:
        raise TrackingError(
            "PostEx response belongs to a different tracking number or has no order."
        )
    rows = order.get("transactionStatusHistory") or []
    if not isinstance(rows, list) or len(rows) > 2000:
        raise TrackingError("PostEx history response format is not supported.")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise TrackingError("PostEx history response format is not supported.")
        result.append(
            checkpoint(
                row.get("transactionStatusMessage"),
                row.get("updatedAt") or row.get("createDatetime"),
                row.get("transactionStatusMessageCode"),
            )
        )
    # A current summary can lag behind the history. Prefer dated checkpoints.
    # With no history, keep the summary but do not invent a provider timestamp.
    if not result:
        result.append(checkpoint(order.get("transactionStatus")))
    return result


def fetch_tracking(provider, tracking_id, workspace_id):
    if provider in RUN_PROVIDERS:
        data = request_json(
            "POST", RUN_BASE + "TrackOrder.php", payload={"tracking_no": tracking_id}
        )
        if data == []:
            data = request_json(
                "POST", RUN_BASE + "CurrentStatus.php", payload={"tracking_no": tracking_id}
            )
        return "run_courier", parse_run(data, tracking_id)
    if provider == "PostEx":
        if not settings.POSTEX_API_TOKEN:
            raise TrackingError("Configure POSTEX_API_TOKEN in Backend/.env.")
        data = request_json(
            "GET", POSTEX_BASE + quote(tracking_id, safe=""), token=settings.POSTEX_API_TOKEN
        )
        return "postex", parse_postex(data, tracking_id)
    raise TrackingError("Auto Tracking not available for other shipping services.")
