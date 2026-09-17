"""Shared HTTP behavior for the NCBI-hosted services this tool talks to."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Self

import httpx

from pmc_downloader import __version__

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
QueryParams = Mapping[str, str] | Sequence[tuple[str, str]]
LOGGER = logging.getLogger(__name__)


class NcbiRequestError(RuntimeError):
    """Base error raised when a request to an NCBI-hosted service fails."""


class RateLimiter:
    """Enforce a minimum interval between the start of HTTP requests."""

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last_request: float | None = None

    def wait(self) -> None:
        """Wait until another request can be started."""
        now = self._clock()
        if self._last_request is not None:
            remaining = self.interval - (now - self._last_request)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last_request = now


class HttpSession:
    """A rate-limited, retrying HTTP client for one NCBI-hosted service."""

    service = "NCBI"
    error = NcbiRequestError

    def __init__(
        self,
        *,
        delay: float = 0.34,
        retries: int = 3,
        email: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        user_agent = f"pmc-downloader/{__version__}"
        if email:
            user_agent = f"{user_agent} (mailto:{email})"

        self._email = email
        self._sleep = sleep
        self._limiter = RateLimiter(delay, sleep=sleep)
        self._retries = retries
        self._http = httpx.Client(
            follow_redirects=True,
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(60.0, connect=10.0),
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._http.close()

    def _get(self, url: str, *, params: QueryParams | None = None) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            self._limiter.wait()
            try:
                response = self._http.get(url, params=params)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise self.error(
                            f"{self.service} request failed with HTTP "
                            f"{response.status_code}: {response.url}"
                        ) from exc
                    return response
                last_error = self.error(
                    f"{self.service} request failed with HTTP "
                    f"{response.status_code}: {response.url}"
                )
                retry_after = retry_after_seconds(response)
                response.close()
                if attempt < self._retries:
                    wait = retry_after if retry_after is not None else 2**attempt
                    LOGGER.warning("Request failed; retrying in %.2f seconds: %s", wait, last_error)
                    self._sleep(wait)
                    continue

            if attempt < self._retries:
                wait = 2**attempt
                LOGGER.warning("Request failed; retrying in %.2f seconds: %s", wait, last_error)
                self._sleep(wait)

        raise self.error(
            f"{self.service} request failed after {self._retries + 1} attempts: {url}: {last_error}"
        ) from last_error


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Return the delay requested by a Retry-After header, when it supplies one."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
