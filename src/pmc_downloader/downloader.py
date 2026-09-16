"""High-level PMC article download workflow."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pmc_downloader.client import PmcClient, PmcDownloadError, s3_to_https

SUPPORTED_FILE_TYPES = ("pdf", "xml", "txt", "json")
LOGGER = logging.getLogger(__name__)
METADATA_URL_FIELDS = {
    "pdf": "pdf_url",
    "xml": "xml_url",
    "txt": "text_url",
}


@dataclass(frozen=True)
class DownloadResult:
    """Outcome for one requested article version and file type."""

    pmcid: str
    version: str | None
    file_type: str | None
    status: str
    path: Path | None = None
    detail: str | None = None


def download_articles(
    client: PmcClient,
    pmcids: list[str],
    file_types: tuple[str, ...],
    output_dir: Path,
    on_pmcid_complete: Callable[[str, list[DownloadResult]], None] | None = None,
) -> list[DownloadResult]:
    """Download requested file types for the highest-numbered version of each PMCID."""
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[DownloadResult] = []

    for pmcid in pmcids:
        LOGGER.info("Processing %s", pmcid)
        pmcid_results = _download_article(client, pmcid, file_types, output_dir)
        results.extend(pmcid_results)
        if on_pmcid_complete is not None:
            on_pmcid_complete(pmcid, pmcid_results)

    return results


def _download_article(
    client: PmcClient,
    pmcid: str,
    file_types: tuple[str, ...],
    output_dir: Path,
) -> list[DownloadResult]:
    try:
        versions = client.list_versions(pmcid)
    except PmcDownloadError as exc:
        return [DownloadResult(pmcid, None, None, "failed", detail=str(exc))]

    if not versions:
        return [
            DownloadResult(
                pmcid,
                None,
                None,
                "not-found",
                detail="no downloadable versions found in the PMC Open Data bucket",
            )
        ]

    version = versions[-1]
    try:
        metadata, metadata_bytes = client.get_metadata(version)
    except PmcDownloadError as exc:
        return [DownloadResult(pmcid, version, None, "failed", detail=str(exc))]

    results: list[DownloadResult] = []
    for file_type in file_types:
        if file_type == "json":
            results.append(_save_metadata(pmcid, version, metadata, metadata_bytes, output_dir))
            continue

        field = METADATA_URL_FIELDS[file_type]
        source = metadata.get(field)
        if not isinstance(source, str) or not source:
            results.append(
                DownloadResult(
                    pmcid,
                    version,
                    file_type,
                    "unavailable",
                    detail=f"metadata does not provide {field}",
                )
            )
            continue

        try:
            filename = Path(url_path(source)).name
            downloaded = client.download(source, output_dir / filename)
        except (OSError, PmcDownloadError) as exc:
            results.append(DownloadResult(pmcid, version, file_type, "failed", detail=str(exc)))
            continue

        status = "downloaded" if downloaded.downloaded else "skipped"
        results.append(
            DownloadResult(
                pmcid,
                version,
                file_type,
                status,
                path=downloaded.path,
                detail=f"{downloaded.size} bytes",
            )
        )
    return results


def url_path(source: str) -> str:
    """Return the decoded object path from a validated metadata URL."""
    from urllib.parse import unquote, urlsplit

    return unquote(urlsplit(s3_to_https(source)).path)


def _save_metadata(
    pmcid: str,
    version: str,
    metadata: dict[str, object],
    original: bytes,
    output_dir: Path,
) -> DownloadResult:
    destination = output_dir / f"{version}.json"
    payload = original
    try:
        json.loads(payload)
    except (TypeError, ValueError):
        payload = json.dumps(metadata, indent=2, sort_keys=True).encode() + b"\n"

    try:
        if destination.is_file() and destination.read_bytes() == payload:
            return DownloadResult(
                pmcid, version, "json", "skipped", destination, f"{len(payload)} bytes"
            )
        part_path = destination.with_name(f".{destination.name}.{os.getpid()}.part")
        try:
            part_path.write_bytes(payload)
            os.replace(part_path, destination)
        finally:
            part_path.unlink(missing_ok=True)
    except OSError as exc:
        return DownloadResult(pmcid, version, "json", "failed", detail=str(exc))

    return DownloadResult(
        pmcid, version, "json", "downloaded", destination, f"{len(payload)} bytes"
    )
