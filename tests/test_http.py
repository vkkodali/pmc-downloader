from __future__ import annotations

import httpx
import pytest

from pmc_downloader.http import HttpSession, NcbiRequestError, RateLimiter


def test_rate_limiter_waits_for_remaining_interval() -> None:
    current = [10.0]
    waits: list[float] = []

    def clock() -> float:
        return current[0]

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        current[0] += seconds

    limiter = RateLimiter(0.5, clock=clock, sleep=sleep)
    limiter.wait()
    current[0] += 0.2
    limiter.wait()

    assert waits == pytest.approx([0.3])


def test_get_raises_service_specific_error() -> None:
    class Service(HttpSession):
        service = "Example"
        error = type("ExampleError", (NcbiRequestError,), {})

    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    with (
        Service(delay=0, transport=transport) as session,
        pytest.raises(Service.error, match="Example request failed with HTTP 404"),
    ):
        session._get("https://example.invalid/resource")
