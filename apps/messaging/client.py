"""Server-configured WAHA origin only; never accept remote URLs or credentials from tenants."""

import json
from urllib.parse import urlsplit

import urllib3
from django.conf import settings

http = urllib3.PoolManager()


class WahaError(Exception):
    def __init__(self, message, *, status=0, uncertain=False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain


def configured():
    return bool(settings.WAHA_ENABLED and settings.WAHA_BASE_URL and settings.WAHA_API_KEY)


def request(method, path, payload=None):
    if not configured():
        raise WahaError("Configure WAHA_ENABLED, WAHA_BASE_URL and WAHA_API_KEY on the server.")
    origin = urlsplit(settings.WAHA_BASE_URL)
    if (
        (
            origin.scheme != "https"
            and not (settings.DEBUG and settings.WAHA_ALLOW_HTTP and origin.scheme == "http")
        )
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
    ):
        raise WahaError(
            "WAHA requires a trusted HTTPS base URL without credentials or query parameters."
        )
    response = None
    sending = path in {"/api/sendText", "/api/sendImage", "/api/sendFile", "/api/sendVideo"}
    try:
        response = http.request(
            method,
            settings.WAHA_BASE_URL.rstrip("/") + path,
            headers={
                "X-Api-Key": settings.WAHA_API_KEY,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            body=json.dumps(payload).encode() if payload is not None else None,
            timeout=urllib3.Timeout(connect=3, read=12),
            retries=False,
            redirect=False,
            preload_content=False,
        )
        if not 200 <= response.status < 300:
            raise WahaError(
                f"WAHA returned HTTP {response.status}. Check the session and server configuration.",
                status=response.status,
                uncertain=sending and response.status >= 500,
            )
        raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError()
        return json.loads(raw) if raw else {}
    except (urllib3.exceptions.HTTPError, OSError):
        raise WahaError("WAHA connection timed out or failed.", uncertain=sending) from None
    except (ValueError, UnicodeError):
        raise WahaError("WAHA returned an invalid response.", uncertain=sending) from None
    finally:
        if response is not None:
            response.close()
            response.release_conn()
