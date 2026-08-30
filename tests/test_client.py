from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from pmc_downloader.client import (
    BUCKET_URL,
    PmcClient,
    PmcDownloadError,
    RateLimiter,
    md5_from_url,
    s3_to_https,
)


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


def test_list_versions_uses_exact_prefix_and_numeric_sorting() -> None:
    listing = b"""<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <CommonPrefixes><Prefix>PMC123.10/</Prefix></CommonPrefixes>
      <CommonPrefixes><Prefix>PMC123.2/</Prefix></CommonPrefixes>
    </ListBucketResult>"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["prefix"] == "PMC123."
        assert request.url.params["delimiter"] == "/"
        return httpx.Response(200, content=listing)

    with PmcClient(delay=0, transport=httpx.MockTransport(handler)) as client:
        assert client.list_versions("PMC123") == ["PMC123.2", "PMC123.10"]


def test_list_versions_retries_retryable_response() -> None:
    calls = 0
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(200, content=b"<ListBucketResult />")

    with PmcClient(
        delay=0,
        retries=1,
        transport=httpx.MockTransport(handler),
        sleep=waits.append,
    ) as client:
        assert client.list_versions("PMC123") == []

    assert calls == 2
    assert waits == [0.0]


def test_download_streams_validates_and_skips_existing_file(tmp_path: Path) -> None:
    payload = b"%PDF-1.7\ntest PDF\n"
    checksum = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/PMC123.1/PMC123.1.pdf"
        return httpx.Response(200, content=payload)

    destination = tmp_path / "PMC123.1.pdf"
    source = f"s3://pmc-oa-opendata/PMC123.1/PMC123.1.pdf?md5={checksum}"
    with PmcClient(delay=0, transport=httpx.MockTransport(handler)) as client:
        first = client.download(source, destination)
        second = client.download(source, destination)

    assert first.downloaded is True
    assert second.downloaded is False
    assert first.size == second.size == len(payload)
    assert destination.read_bytes() == payload
    assert calls == 1
    assert not list(tmp_path.glob("*.part"))


def test_download_retries_checksum_mismatch(tmp_path: Path) -> None:
    good = b"expected"
    checksum = hashlib.md5(good, usedforsecurity=False).hexdigest()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"bad" if calls == 1 else good)

    source = f"s3://pmc-oa-opendata/PMC123.1/PMC123.1.txt?md5={checksum}"
    destination = tmp_path / "PMC123.1.txt"
    with PmcClient(
        delay=0,
        retries=1,
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
    ) as client:
        result = client.download(source, destination)

    assert result.downloaded is True
    assert destination.read_bytes() == good
    assert calls == 2


def test_get_metadata_rejects_non_object_json() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[]))

    with (
        PmcClient(delay=0, transport=transport) as client,
        pytest.raises(PmcDownloadError, match="unexpected JSON"),
    ):
        client.get_metadata("PMC123.1")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "s3://pmc-oa-opendata/PMC123.1/file%20name.pdf?md5=abc",
            f"{BUCKET_URL}/PMC123.1/file%20name.pdf?md5=abc",
        ),
        (f"{BUCKET_URL}/PMC123.1/PMC123.1.pdf", f"{BUCKET_URL}/PMC123.1/PMC123.1.pdf"),
    ],
)
def test_s3_to_https(source: str, expected: str) -> None:
    assert s3_to_https(source) == expected


def test_s3_to_https_rejects_unexpected_bucket() -> None:
    with pytest.raises(PmcDownloadError, match="refusing unexpected"):
        s3_to_https("s3://untrusted-bucket/file.pdf")


def test_md5_from_url_validates_digest() -> None:
    digest = "648db0ce6ce56688995af0450090db28"
    assert md5_from_url(f"{BUCKET_URL}/file?md5={digest}") == digest
    assert md5_from_url(f"{BUCKET_URL}/file?md5=not-a-digest") is None
