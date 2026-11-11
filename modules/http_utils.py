"""HTTP helpers with backoff. Tokens are stripped from logged URLs."""

from __future__ import annotations

import logging
import random
import re
import time

import requests

from modules.errors import ProspectingError

LOGGER = logging.getLogger("prospecting.http")
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HttpError(ProspectingError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def redact(text: str) -> str:
    cleaned = re.sub(r"(?i)(token=)[^&\s]+", r"\1***", text)
    cleaned = re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1***", cleaned)
    cleaned = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+=*", "Bearer ***", cleaned)
    return cleaned


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    retries: int = 4,
    backoff_seconds: float = 1.0,
    sleep=time.sleep,
    **kwargs,
) -> requests.Response:
    """Retry transient HTTP failures. 401 and 403 are returned to the caller."""
    delay = backoff_seconds
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == retries:
                break
            LOGGER.warning("Request failed (%s). Retry %s/%s.", redact(str(exc)), attempt, retries)
            sleep(delay + random.uniform(0, 0.25))
            delay *= 2
            continue
        if response.status_code in RETRYABLE_STATUS and attempt < retries:
            retry_after = response.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else delay
            except ValueError:
                wait = delay
            wait = min(wait, 60)
            LOGGER.warning(
                "%s %s returned %s. Retry %s/%s in %.1fs.",
                method.upper(),
                redact(url),
                response.status_code,
                attempt,
                retries,
                wait,
            )
            sleep(wait + random.uniform(0, 0.25))
            delay *= 2
            last_error = HttpError(
                f"{method.upper()} {redact(url)} returned {response.status_code}.",
                response.status_code,
            )
            continue
        return response
    raise HttpError(f"{method.upper()} {redact(url)} failed: {last_error}")
