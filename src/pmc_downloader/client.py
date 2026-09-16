"""HTTP client for the public PMC Open Data S3 bucket."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx

from pmc_downloader import __version__

BUCKET = "pmc-oa-opendata"
BUCKET_HOST = f"{BUCKET}.s3.amazonaws.com"
BUCKET_URL = f"https://{BUCKET_HOST}"
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
LOGGER = logging.getLogger(__name__)


class PmcDownloadError(RuntimeError):
    """Base error raised for PMC lookup and download failures."""


class ChecksumMismatchError(PmcDownloadError):
    """Raised when a downloaded object's checksum does not match PMC metadata."""


class _RetryableDownloadError(PmcDownloadError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class DownloadedObject:
    """Information about a successfully downloaded or already-present object."""

    path: Path
    downloaded: bool
    size: int


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


class PmcClient:
    """Retrieve article metadata and objects without AWS credentials."""

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

        self._sleep = sleep
        self._limiter = RateLimiter(delay, sleep=sleep)
        self._retries = retries
        self._http = httpx.Client(
            follow_redirects=True,
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(60.0, connect=10.0),
            transport=transport,
        )

    def __enter__(self) -> PmcClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._http.close()

    def list_versions(self, pmcid: str) -> list[str]:
        """Return all S3 version prefixes belonging to a PMCID."""
        response = self._get(
            f"{BUCKET_URL}/",
            params={"list-type": "2", "prefix": f"{pmcid}.", "delimiter": "/"},
        )
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise PmcDownloadError(f"PMC returned an invalid bucket listing for {pmcid}") from exc

        prefixes = [
            element.text.rstrip("/")
            for element in root.findall("{*}CommonPrefixes/{*}Prefix")
            if element.text and element.text.rstrip("/").startswith(f"{pmcid}.")
        ]
        return sorted(set(prefixes), key=_version_number)

    def get_metadata(self, version: str) -> tuple[dict[str, object], bytes]:
        """Return parsed and original JSON metadata for an article version."""
        url = f"{BUCKET_URL}/metadata/{quote(version, safe='')}.json"
        response = self._get(url)
        try:
            value = response.json()
        except ValueError as exc:
            raise PmcDownloadError(f"PMC returned invalid JSON metadata for {version}") from exc
        if not isinstance(value, dict):
            raise PmcDownloadError(f"PMC returned unexpected JSON metadata for {version}")
        return value, response.content

    def download(self, source_url: str, destination: Path) -> DownloadedObject:
        """Stream one S3 object to disk atomically and validate its MD5 when supplied."""
        url = s3_to_https(source_url)
        expected_md5 = md5_from_url(url)

        if destination.is_file() and expected_md5:
            actual_md5, size = _file_md5(destination)
            if actual_md5 == expected_md5:
                LOGGER.info("Already present: %s (%d bytes)", destination, size)
                return DownloadedObject(destination, downloaded=False, size=size)

        part_path = destination.with_name(f".{destination.name}.{os.getpid()}.part")
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            part_path.unlink(missing_ok=True)
            try:
                LOGGER.info(
                    "Downloading %s to %s (attempt %d of %d)",
                    url,
                    destination,
                    attempt + 1,
                    self._retries + 1,
                )
                size = self._download_once(url, part_path, expected_md5)
                os.replace(part_path, destination)
                LOGGER.info("Downloaded %s (%d bytes)", destination, size)
                return DownloadedObject(destination, downloaded=True, size=size)
            except (httpx.TransportError, ChecksumMismatchError, _RetryableDownloadError) as exc:
                last_error = exc
                part_path.unlink(missing_ok=True)
                if attempt == self._retries:
                    break
                retry_after = exc.retry_after if isinstance(exc, _RetryableDownloadError) else None
                wait = retry_after if retry_after is not None else 2**attempt
                LOGGER.warning("Download attempt failed; retrying in %.2f seconds: %s", wait, exc)
                self._sleep(wait)
            except Exception:
                part_path.unlink(missing_ok=True)
                raise

        raise PmcDownloadError(
            f"failed to download {url} after {self._retries + 1} attempts: {last_error}"
        ) from last_error

    def _get(self, url: str, *, params: dict[str, str] | None = None) -> httpx.Response:
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
                        raise PmcDownloadError(
                            f"PMC request failed with HTTP {response.status_code}: {response.url}"
                        ) from exc
                    return response
                last_error = PmcDownloadError(
                    f"PMC request failed with HTTP {response.status_code}: {response.url}"
                )
                retry_after = _retry_after_seconds(response)
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

        raise PmcDownloadError(
            f"PMC request failed after {self._retries + 1} attempts: {url}: {last_error}"
        ) from last_error

    def _download_once(self, url: str, part_path: Path, expected_md5: str | None) -> int:
        self._limiter.wait()
        digest = hashlib.md5(usedforsecurity=False)
        size = 0
        with self._http.stream("GET", url) as response:
            if response.status_code in RETRYABLE_STATUS_CODES:
                raise _RetryableDownloadError(
                    f"PMC request failed with HTTP {response.status_code}: {response.url}",
                    retry_after=_retry_after_seconds(response),
                )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise PmcDownloadError(
                    f"PMC request failed with HTTP {response.status_code}: {response.url}"
                ) from exc
            with part_path.open("wb") as output:
                size = _copy_response(response, output, digest)

        if expected_md5 and digest.hexdigest() != expected_md5:
            raise ChecksumMismatchError(
                f"checksum mismatch for {url}: expected {expected_md5}, got {digest.hexdigest()}"
            )
        return size


def s3_to_https(value: str) -> str:
    """Convert and validate a PMC S3 object URL."""
    parsed = urlsplit(value)
    if parsed.scheme == "s3" and parsed.netloc == BUCKET:
        path = quote(unquote(parsed.path), safe="/")
        return urlunsplit(("https", BUCKET_HOST, path, parsed.query, ""))
    if parsed.scheme == "https" and parsed.netloc == BUCKET_HOST:
        return value
    raise PmcDownloadError(f"refusing unexpected object URL in PMC metadata: {value}")


def md5_from_url(value: str) -> str | None:
    """Extract a valid MD5 digest from a PMC object URL."""
    candidate = parse_qs(urlsplit(value).query).get("md5", [None])[0]
    if (
        candidate
        and len(candidate) == 32
        and all(char in "0123456789abcdefABCDEF" for char in candidate)
    ):
        return candidate.lower()
    return None


def _version_number(version: str) -> int:
    try:
        return int(version.rsplit(".", 1)[1])
    except (IndexError, ValueError):
        return 0


def _file_md5(path: Path) -> tuple[str, int]:
    digest = hashlib.md5(usedforsecurity=False)
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _copy_response(response: httpx.Response, output: BinaryIO, digest: object) -> int:
    size = 0
    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
        output.write(chunk)
        digest.update(chunk)  # type: ignore[attr-defined]
        size += len(chunk)
    return size


def _retry_after_seconds(response: httpx.Response) -> float | None:
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
