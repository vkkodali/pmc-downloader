#!/usr/bin/env python3
"""Download PMC PDFs associated with the first 50 Bos taurus Gene hits.

This is a standalone analysis workflow.  It searches NCBI Gene with ESearch,
gets all PubMed IDs from the NCBI Datasets API, and converts them in descending
numeric order until it has collected 50 PMC IDs. It then invokes
``pmc-download`` once per gene.

Run from the repository root, preferably with a contact email:

    uv run python analysis/download_bos_taurus_gene_pdfs.py \
        --email researcher@example.org

The output is resumable: ``pmc-download`` validates and skips PDFs that are
already present.  A nonzero ``pmc-download`` exit code is recorded but does not
stop later genes, because some PubMed Central records are not downloadable as
PDFs from the PMC Open Data bucket.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
DATASETS_PUBMED_URL = "https://api.ncbi.nlm.nih.gov/datasets/v2/gene/id/{gene_id}/pubmedids"
ID_CONVERTER_URL = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
DEFAULT_QUERY = '"bos taurus"[organism]'
TOOL_NAME = "bos_taurus_gene_pmc_analysis"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
PMCID_PATTERN = re.compile(r"PMC[1-9][0-9]*", re.IGNORECASE)

LOGGER = logging.getLogger(TOOL_NAME)


class NcbiApiError(RuntimeError):
    """An NCBI service request failed or returned an unexpected payload."""


class RateLimitedNcbiClient:
    """Small JSON client with sequential rate limiting and retry/backoff."""

    def __init__(
        self,
        *,
        email: str | None,
        delay: float,
        retries: int,
        timeout: float,
    ) -> None:
        self.email = email
        self._delay = delay
        self._retries = retries
        self._last_request_started: float | None = None
        contact = f" ({email})" if email else ""
        self._client = httpx.Client(
            headers={
                "Accept": "application/json",
                "User-Agent": f"{TOOL_NAME}/1.0{contact}",
            },
            follow_redirects=True,
            timeout=timeout,
        )

    def __enter__(self) -> RateLimitedNcbiClient:
        return self

    def __exit__(self, *args: object) -> None:
        self._client.close()

    def get_json(
        self,
        url: str,
        *,
        service: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """GET and decode a JSON object, retrying transient failures."""
        for attempt in range(self._retries + 1):
            self._respect_rate_limit()
            self._last_request_started = time.monotonic()
            try:
                response = self._client.get(url, params=params)
            except httpx.RequestError as exc:
                if attempt == self._retries:
                    raise NcbiApiError(
                        f"{service} request failed after {attempt + 1} attempts"
                    ) from exc
                self._sleep_before_retry(service, attempt, None)
                continue

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self._retries:
                self._sleep_before_retry(service, attempt, response.headers.get("Retry-After"))
                continue
            if not response.is_success:
                raise NcbiApiError(f"{service} returned HTTP {response.status_code}")

            try:
                payload = response.json()
            except ValueError as exc:
                raise NcbiApiError(f"{service} returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise NcbiApiError(f"{service} returned a non-object JSON response")
            return payload

        raise AssertionError("retry loop exited unexpectedly")

    def _respect_rate_limit(self) -> None:
        if self._last_request_started is None:
            return
        remaining = self._delay - (time.monotonic() - self._last_request_started)
        if remaining > 0:
            time.sleep(remaining)

    @staticmethod
    def _sleep_before_retry(service: str, attempt: int, retry_after: str | None) -> None:
        try:
            delay = float(retry_after) if retry_after is not None else 2**attempt
        except ValueError:
            delay = 2**attempt
        delay = min(max(delay, 0.0), 60.0)
        LOGGER.warning("%s request failed transiently; retrying in %.1f seconds", service, delay)
        time.sleep(delay)


@dataclass(frozen=True)
class ConversionResult:
    pmcids: list[str]
    examined_pmids: list[int]
    unmapped_pmids: list[int]


@dataclass
class GeneResult:
    rank: int
    gene_id: str
    total_pmids: int | str = 0
    examined_pmids: int = 0
    mapped_pmcids: int = 0
    unmapped_pmids: int = 0
    pdf_files: int = 0
    download_exit_code: int | str = "not-run"
    status: str = "pending"


def search_gene_ids(
    client: RateLimitedNcbiClient,
    *,
    query: str,
    limit: int,
) -> list[str]:
    """Return the first Gene IDs in the ordering supplied by ESearch."""
    params = {
        "db": "gene",
        "term": query,
        "retmax": str(limit),
        "retmode": "json",
        "tool": TOOL_NAME,
    }
    if client.email:
        params["email"] = client.email

    payload = client.get_json(ESEARCH_URL, service="Entrez ESearch", params=params)
    result = payload.get("esearchresult")
    if not isinstance(result, dict) or not isinstance(result.get("idlist"), list):
        raise NcbiApiError("Entrez ESearch response has no esearchresult.idlist")

    gene_ids = [str(value) for value in result["idlist"]]
    if any(not gene_id.isdigit() or int(gene_id) < 1 for gene_id in gene_ids):
        raise NcbiApiError("Entrez ESearch returned an invalid Gene ID")
    if len(gene_ids) < limit:
        LOGGER.warning("ESearch returned %d Gene IDs; %d were requested", len(gene_ids), limit)
    return gene_ids[:limit]


def fetch_pmids_descending(
    client: RateLimitedNcbiClient,
    *,
    gene_id: str,
) -> list[int]:
    """Fetch all of a gene's unique PMIDs in descending numeric order."""
    url = DATASETS_PUBMED_URL.format(gene_id=gene_id)
    payload = client.get_json(url, service=f"Datasets GeneID {gene_id}")
    raw_pmids = payload.get("pubmed_ids")
    if not isinstance(raw_pmids, list):
        raise NcbiApiError(f"Datasets response for GeneID {gene_id} has no pubmed_ids list")

    pmids: set[int] = set()
    for value in raw_pmids:
        if isinstance(value, bool):
            raise NcbiApiError(f"Datasets returned an invalid PMID for GeneID {gene_id}")
        try:
            pmid = int(value)
        except (TypeError, ValueError) as exc:
            raise NcbiApiError(f"Datasets returned an invalid PMID for GeneID {gene_id}") from exc
        if pmid < 1:
            raise NcbiApiError(f"Datasets returned an invalid PMID for GeneID {gene_id}")
        pmids.add(pmid)

    return sorted(pmids, reverse=True)


def convert_pmids_to_pmcids(
    client: RateLimitedNcbiClient,
    pmids: list[int],
    *,
    limit: int,
) -> ConversionResult:
    """Convert descending PMIDs until at most ``limit`` unique PMC IDs are found."""
    if not pmids:
        return ConversionResult(pmcids=[], examined_pmids=[], unmapped_pmids=[])

    pmcids: list[str] = []
    seen_pmcids: set[str] = set()
    examined_pmids: list[int] = []
    unmapped_pmids: list[int] = []
    start = 0
    while start < len(pmids) and len(pmcids) < limit:
        # Request no more PMIDs than the number of PMC IDs still needed. This
        # prevents a successful batch from converting past the requested cap.
        batch_size = min(200, limit - len(pmcids), len(pmids) - start)
        batch = pmids[start : start + batch_size]
        start += batch_size
        params = {
            "ids": ",".join(map(str, batch)),
            "idtype": "pmid",
            "format": "json",
            "tool": TOOL_NAME,
        }
        if client.email:
            params["email"] = client.email
        payload = client.get_json(
            ID_CONVERTER_URL,
            service="PMC ID Converter",
            params=params,
        )
        if payload.get("status") != "ok" or not isinstance(payload.get("records"), list):
            raise NcbiApiError("PMC ID Converter returned an unexpected response")

        pmcid_by_pmid: dict[int, str] = {}
        for record in payload["records"]:
            if not isinstance(record, dict):
                raise NcbiApiError("PMC ID Converter returned an invalid record")
            pmcid_value = record.get("pmcid")
            pmid_value = record.get("pmid", record.get("requested-id"))
            if pmcid_value is None:
                continue
            try:
                pmid = int(pmid_value)
            except (TypeError, ValueError) as exc:
                raise NcbiApiError(
                    "PMC ID Converter returned a PMCID without a valid PMID"
                ) from exc
            pmcid = str(pmcid_value).upper()
            if PMCID_PATTERN.fullmatch(pmcid) is None:
                raise NcbiApiError(f"PMC ID Converter returned an invalid PMCID for PMID {pmid}")
            pmcid_by_pmid[pmid] = pmcid

        for pmid in batch:
            examined_pmids.append(pmid)
            pmcid = pmcid_by_pmid.get(pmid)
            if pmcid is None:
                unmapped_pmids.append(pmid)
            elif pmcid not in seen_pmcids:
                seen_pmcids.add(pmcid)
                pmcids.append(pmcid)

    return ConversionResult(
        pmcids=pmcids,
        examined_pmids=examined_pmids,
        unmapped_pmids=unmapped_pmids,
    )


def write_lines(path: Path, values: list[str] | list[int]) -> None:
    """Write one value per line, including a final newline for nonempty lists."""
    content = "".join(f"{value}\n" for value in values)
    path.write_text(content, encoding="utf-8")


def write_gene_list(output_dir: Path, gene_ids: list[str]) -> None:
    write_lines(output_dir / "gene_ids.txt", gene_ids)
    rows = ["rank\tgene_id\tdirectory\n"]
    rows.extend(
        f"{rank}\t{gene_id}\tGeneID_{gene_id}\n" for rank, gene_id in enumerate(gene_ids, start=1)
    )
    (output_dir / "genes.tsv").write_text("".join(rows), encoding="utf-8")


def write_summary(output_dir: Path, results: list[GeneResult]) -> None:
    fields = list(GeneResult.__dataclass_fields__)
    rows = ["\t".join(fields) + "\n"]
    for result in results:
        values = asdict(result)
        rows.append("\t".join(str(values[field]) for field in fields) + "\n")
    (output_dir / "summary.tsv").write_text("".join(rows), encoding="utf-8")


def resolve_pmc_download(executable: str) -> list[str]:
    """Resolve the CLI, falling back to this interpreter's installed module."""
    resolved = shutil.which(executable)
    if resolved:
        return [resolved]
    if executable == "pmc-download" and importlib.util.find_spec("pmc_downloader") is not None:
        LOGGER.warning("pmc-download was not on PATH; using python -m pmc_downloader")
        return [sys.executable, "-m", "pmc_downloader"]
    raise RuntimeError(
        f"cannot find {executable!r}; run with 'uv run python ...' or pass --pmc-download"
    )


def run_pmc_download(
    command_prefix: list[str],
    *,
    pmcid_file: Path,
    gene_dir: Path,
    email: str | None,
) -> int:
    command = [
        *command_prefix,
        "--input-file",
        str(pmcid_file),
        "--types",
        "pdf",
        "--output-dir",
        str(gene_dir),
    ]
    if email:
        command.extend(["--email", email])
    completed = subprocess.run(command, check=False)
    return completed.returncode


def configure_logging(output_dir: Path) -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(output_dir / "analysis.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(stream_handler)
    LOGGER.addHandler(file_handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("bos_taurus_gene_pdfs"),
        help="analysis output directory (default: ./bos_taurus_gene_pdfs)",
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("NCBI_EMAIL"),
        help="contact email for NCBI requests (or set NCBI_EMAIL)",
    )
    parser.add_argument("--query", default=DEFAULT_QUERY, help=argparse.SUPPRESS)
    parser.add_argument("--gene-limit", type=int, default=50, help=argparse.SUPPRESS)
    parser.add_argument(
        "--pmcid-limit",
        type=int,
        default=50,
        help="maximum PMC IDs and PDFs per gene (default: 50)",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.34,
        help="minimum seconds between analysis API requests (default: 0.34)",
    )
    parser.add_argument("--retries", type=int, default=3, help="transient API retries (default: 3)")
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="timeout in seconds for each analysis API request (default: 30)",
    )
    parser.add_argument(
        "--pmc-download",
        default="pmc-download",
        metavar="EXECUTABLE",
        help="pmc-download executable name or path (default: pmc-download)",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    if args.gene_limit < 1 or args.pmcid_limit < 1:
        raise ValueError("gene and PMCID limits must be positive")
    if args.request_delay < 0 or args.retries < 0 or args.timeout <= 0:
        raise ValueError("request delay/retries must be nonnegative and timeout must be positive")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(output_dir)
    if not args.email:
        LOGGER.warning("No contact email supplied; NCBI requests one for programmatic API use")

    downloader = resolve_pmc_download(args.pmc_download)
    LOGGER.info("Searching NCBI Gene for %s", args.query)
    with RateLimitedNcbiClient(
        email=args.email,
        delay=args.request_delay,
        retries=args.retries,
        timeout=args.timeout,
    ) as client:
        gene_ids = search_gene_ids(client, query=args.query, limit=args.gene_limit)
        write_gene_list(output_dir, gene_ids)
        gene_dirs = [output_dir / f"GeneID_{gene_id}" for gene_id in gene_ids]
        for gene_dir in gene_dirs:
            gene_dir.mkdir(parents=True, exist_ok=True)

        results: list[GeneResult] = []
        processing_errors = 0
        partial_downloads = 0
        for rank, (gene_id, gene_dir) in enumerate(zip(gene_ids, gene_dirs, strict=True), start=1):
            result = GeneResult(rank=rank, gene_id=gene_id)
            results.append(result)
            LOGGER.info("[%d/%d] Processing GeneID %s", rank, len(gene_ids), gene_id)
            try:
                pmids = fetch_pmids_descending(client, gene_id=gene_id)
                result.total_pmids = len(pmids)
                write_lines(gene_dir / "pmids.txt", pmids)

                converted = convert_pmids_to_pmcids(
                    client,
                    pmids,
                    limit=args.pmcid_limit,
                )
                result.examined_pmids = len(converted.examined_pmids)
                result.mapped_pmcids = len(converted.pmcids)
                result.unmapped_pmids = len(converted.unmapped_pmids)
                pmcid_file = gene_dir / "pmcids.txt"
                write_lines(pmcid_file, converted.pmcids)
                write_lines(gene_dir / "examined_pmids.txt", converted.examined_pmids)
                write_lines(gene_dir / "unmapped_pmids.txt", converted.unmapped_pmids)

                if converted.pmcids:
                    exit_code = run_pmc_download(
                        downloader,
                        pmcid_file=pmcid_file,
                        gene_dir=gene_dir,
                        email=args.email,
                    )
                    result.download_exit_code = exit_code
                    if exit_code == 0:
                        result.status = "complete"
                    else:
                        result.status = "download-partial"
                        partial_downloads += 1
                        LOGGER.warning(
                            "pmc-download exited %d for GeneID %s; continuing",
                            exit_code,
                            gene_id,
                        )
                else:
                    result.status = "no-pmcids"
                result.pdf_files = sum(1 for _ in gene_dir.glob("*.pdf"))
            except (NcbiApiError, OSError, RuntimeError) as exc:
                processing_errors += 1
                result.status = "error"
                result.total_pmids = result.total_pmids or "unknown"
                LOGGER.error("GeneID %s failed: %s", gene_id, exc)
            finally:
                write_summary(output_dir, results)

    LOGGER.info(
        "Finished %d genes: %d processing errors, %d partial pmc-download runs. Summary: %s",
        len(gene_ids),
        processing_errors,
        partial_downloads,
        output_dir / "summary.tsv",
    )
    return 1 if processing_errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (NcbiApiError, OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
