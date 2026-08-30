"""Command-line interface for PMC Downloader."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from pmc_downloader import __version__
from pmc_downloader.client import PmcClient
from pmc_downloader.downloader import SUPPORTED_FILE_TYPES, DownloadResult, download_articles

DEFAULT_DELAY_SECONDS = 0.34
PMCID_PATTERN = re.compile(r"(?:PMC)?([0-9]+)", re.IGNORECASE)


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="pmc-download",
        description=(
            "Download article files for one or more PMC IDs from the public PMC Open Data bucket. "
            "Every available article version is downloaded."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "pmcids",
        nargs="?",
        help="comma-delimited PMC IDs, such as PMC10009416,PMC12855588",
    )
    source.add_argument(
        "-i",
        "--input-file",
        type=Path,
        help="path to a text file containing one PMC ID per line",
    )
    parser.add_argument(
        "-t",
        "--types",
        "--file-types",
        default="pdf",
        metavar="TYPES",
        help="comma-delimited file types: pdf, xml, txt, json (default: pdf)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("downloads"),
        help="directory in which to store files (default: ./downloads)",
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("NCBI_EMAIL"),
        help="contact email to include in the HTTP User-Agent (or set NCBI_EMAIL)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def normalize_pmcid(value: str) -> str:
    """Validate and normalize one PMC accession ID."""
    candidate = value.strip()
    match = PMCID_PATTERN.fullmatch(candidate)
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"invalid PMC ID: {value!r}")
    return f"PMC{int(match.group(1))}"


def parse_pmcids(value: str) -> list[str]:
    """Parse a comma-delimited list of PMC IDs, retaining input order."""
    parts = value.split(",")
    if any(not part.strip() for part in parts):
        raise ValueError("PMC ID list contains an empty value")
    return _deduplicate(normalize_pmcid(part) for part in parts)


def read_pmcids(path: Path) -> list[str]:
    """Read one PMC ID per non-empty line from a UTF-8 text file."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ValueError(f"could not read input file {path}: {exc}") from exc

    pmcids: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            pmcids.append(normalize_pmcid(line))
        except ValueError as exc:
            raise ValueError(f"{path}:{line_number}: {exc}") from exc
    if not pmcids:
        raise ValueError(f"input file contains no PMC IDs: {path}")
    return _deduplicate(pmcids)


def parse_file_types(value: str) -> tuple[str, ...]:
    """Validate a comma-delimited set of supported article file types."""
    values = [item.strip().lower().lstrip(".") for item in value.split(",")]
    if any(not item for item in values):
        raise ValueError("file type list contains an empty value")
    unsupported = [item for item in values if item not in SUPPORTED_FILE_TYPES]
    if unsupported:
        supported = ", ".join(SUPPORTED_FILE_TYPES)
        raise ValueError(
            f"unsupported file type(s): {', '.join(unsupported)}; choose from {supported}"
        )
    return tuple(_deduplicate(values))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line application."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        pmcids = read_pmcids(args.input_file) if args.input_file else parse_pmcids(args.pmcids)
        file_types = parse_file_types(args.types)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        if not args.output_dir.is_dir():
            raise ValueError(f"output path is not a directory: {args.output_dir}")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    with PmcClient(delay=DEFAULT_DELAY_SECONDS, email=args.email) as client:
        results = download_articles(client, pmcids, file_types, args.output_dir)

    for result in results:
        print(_format_result(result))

    downloaded = sum(result.status == "downloaded" for result in results)
    skipped = sum(result.status == "skipped" for result in results)
    errors = sum(result.status in {"failed", "not-found", "unavailable"} for result in results)
    print(
        f"Summary: {downloaded} downloaded, {skipped} already present, {errors} unavailable/failed",
        file=sys.stderr if errors else sys.stdout,
    )
    return 1 if errors else 0


def _format_result(result: DownloadResult) -> str:
    target = result.version or result.pmcid
    if result.file_type:
        target = f"{target} {result.file_type}"
    detail = str(result.path) if result.path else result.detail
    if result.path and result.detail:
        detail = f"{result.path} ({result.detail})"
    return f"[{result.status.upper()}] {target}" + (f": {detail}" if detail else "")


def _deduplicate(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


if __name__ == "__main__":
    raise SystemExit(main())
