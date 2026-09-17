"""Command-line interface for PMC Downloader."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

from pmc_downloader import __version__
from pmc_downloader.client import PmcClient
from pmc_downloader.downloader import SUPPORTED_FILE_TYPES, DownloadResult, download_articles
from pmc_downloader.entrez import EntrezClient
from pmc_downloader.identifiers import (
    PMCID,
    Identifier,
    parse_identifiers,
    read_identifiers,
)

DEFAULT_DELAY_SECONDS = 0.34
SUCCESS_STATUSES = frozenset({"downloaded", "skipped"})
MAX_REPORTED_SKIPS = 20
LOGGER = logging.getLogger("pmc_downloader")


class ProgressDisplay:
    """Display PMCID-level progress without mixing in per-file details."""

    def __init__(self, total: int, stream: TextIO) -> None:
        self.total = total
        self.stream = stream
        self.succeeded = 0
        self.failed = 0
        self._interactive = stream.isatty()
        self._started = False

    def start(self) -> None:
        self._write()

    def complete(self, succeeded: bool) -> None:
        if succeeded:
            self.succeeded += 1
        else:
            self.failed += 1
        self._write()

    def close(self) -> None:
        if self._interactive and self._started:
            self.stream.write("\n")
            self.stream.flush()

    def _write(self) -> None:
        remaining = self.total - self.succeeded - self.failed
        line = (
            f"Total: {self.total} | Succeeded: {self.succeeded} | "
            f"Failed: {self.failed} | Remaining: {remaining}"
        )
        if self._interactive:
            self.stream.write(f"\r{line}")
        else:
            self.stream.write(f"{line}\n")
        self.stream.flush()
        self._started = True


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="pmc-download",
        description=(
            "Download article files for one or more PMCIDs or PMIDs from the public PMC Open "
            "Data bucket. PMIDs are converted to PMCIDs with the NCBI Entrez Utilities, and "
            "PMIDs without a PMC copy are reported and skipped. Only the highest-numbered "
            "available article version is downloaded."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "identifiers",
        nargs="?",
        help=(
            "comma-delimited PMCIDs or PMIDs, such as PMC10034327,36969844; an identifier "
            "without the PMC prefix is treated as a PMID"
        ),
    )
    source.add_argument(
        "-i",
        "--input-file",
        type=Path,
        help="path to a text file containing one PMCID or PMID per line",
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
        help=(
            "contact email to include in the HTTP User-Agent and in Entrez Utilities "
            "requests (or set NCBI_EMAIL)"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


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
        identifiers = (
            read_identifiers(args.input_file)
            if args.input_file
            else parse_identifiers(args.identifiers)
        )
        file_types = parse_file_types(args.types)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        if not args.output_dir.is_dir():
            raise ValueError(f"output path is not a directory: {args.output_dir}")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    try:
        log_path, log_handler = _create_log_handler(args.output_dir)
    except OSError as exc:
        parser.error(f"could not create run log in {args.output_dir}: {exc}")

    previous_level = LOGGER.level
    previous_propagate = LOGGER.propagate
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    LOGGER.addHandler(log_handler)
    progress: ProgressDisplay | None = None

    try:
        LOGGER.info("Starting PMC download run")
        LOGGER.info("Output directory: %s", args.output_dir.resolve())
        LOGGER.info(
            "Identifiers (%d): %s",
            len(identifiers),
            ", ".join(identifier.label for identifier in identifiers),
        )
        LOGGER.info("Requested file types: %s", ", ".join(file_types))

        resolution = resolve_identifiers(identifiers, args.email)
        pmcids = resolution.pmcids
        LOGGER.info("PMC IDs (%d): %s", len(pmcids), ", ".join(pmcids))
        progress = ProgressDisplay(len(pmcids), sys.stderr)
        progress.start()

        def pmcid_complete(_pmcid: str, pmcid_results: list[DownloadResult]) -> None:
            for result in pmcid_results:
                LOGGER.info("%s", _format_result(result))
            progress.complete(_pmcid_succeeded(pmcid_results))

        with PmcClient(delay=DEFAULT_DELAY_SECONDS, email=args.email) as client:
            download_articles(
                client,
                pmcids,
                file_types,
                args.output_dir,
                on_pmcid_complete=pmcid_complete,
            )

        succeeded = progress.succeeded
        failed = progress.failed
        LOGGER.info(
            "Run complete: %d total PMC IDs, %d succeeded, %d failed",
            len(pmcids),
            succeeded,
            failed,
        )
    except Exception:
        LOGGER.exception("PMC download run aborted")
        raise
    finally:
        if progress is not None:
            progress.close()
        LOGGER.removeHandler(log_handler)
        log_handler.close()
        LOGGER.setLevel(previous_level)
        LOGGER.propagate = previous_propagate

    skipped = len(resolution.unresolved)
    summary_stream = sys.stderr if failed or skipped else sys.stdout
    print("Summary:", file=summary_stream)
    if resolution.converted or skipped:
        print(f"  Input identifiers: {len(identifiers)}", file=summary_stream)
        print(f"  PMIDs converted to PMCIDs: {len(resolution.converted)}", file=summary_stream)
        print(f"  PMIDs without a PMCID (skipped): {skipped}", file=summary_stream)
        if skipped:
            print(f"    {_skipped_pmids(resolution.unresolved)}", file=summary_stream)
    print(f"  Total PMC IDs: {len(pmcids)}", file=summary_stream)
    print(f"  Succeeded: {succeeded}", file=summary_stream)
    print(f"  Failed: {failed}", file=summary_stream)
    print("  Remaining: 0", file=summary_stream)
    print(f"  Log: {log_path}", file=summary_stream)
    return 1 if failed or skipped else 0


@dataclass(frozen=True)
class Resolution:
    """PMCIDs to download, and the PMIDs that could not be converted to one."""

    pmcids: list[str]
    converted: list[tuple[str, str]]
    unresolved: list[str]


def resolve_identifiers(identifiers: Sequence[Identifier], email: str | None) -> Resolution:
    """Convert every requested PMID to a PMCID, skipping those PMC does not hold."""
    pmids = [identifier.value for identifier in identifiers if identifier.kind != PMCID]
    converted: dict[str, str | None] = {}
    if pmids:
        LOGGER.info("Converting %d PMID(s) to PMCIDs with the NCBI Entrez Utilities", len(pmids))
        with EntrezClient(delay=DEFAULT_DELAY_SECONDS, email=email) as client:
            converted = client.pmids_to_pmcids(pmids)

    pmcids: list[str] = []
    resolved: list[tuple[str, str]] = []
    unresolved: list[str] = []
    for identifier in identifiers:
        if identifier.kind == PMCID:
            pmcids.append(identifier.value)
            continue
        pmcid = converted.get(identifier.value)
        if pmcid is None:
            LOGGER.warning(
                "[NO-PMCID] %s: skipped; PubMed reports no PMC copy of this record",
                identifier.label,
            )
            unresolved.append(identifier.value)
            continue
        LOGGER.info("Converted %s to %s", identifier.label, pmcid)
        resolved.append((identifier.value, pmcid))
        pmcids.append(pmcid)

    return Resolution(_deduplicate(pmcids), resolved, unresolved)


def _skipped_pmids(unresolved: Sequence[str]) -> str:
    shown = [f"PMID:{pmid}" for pmid in unresolved[:MAX_REPORTED_SKIPS]]
    if len(unresolved) > MAX_REPORTED_SKIPS:
        shown.append(f"and {len(unresolved) - MAX_REPORTED_SKIPS} more (see log)")
    return ", ".join(shown)


def _create_log_handler(output_dir: Path) -> tuple[Path, logging.FileHandler]:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    for number in range(1, 10_000):
        suffix = "" if number == 1 else f"-{number}"
        path = output_dir / f"pmc-download-{timestamp}{suffix}.log"
        try:
            handler = logging.FileHandler(path, mode="x", encoding="utf-8")
        except FileExistsError:
            continue
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        return path, handler
    raise OSError("could not choose a unique log filename")


def _pmcid_succeeded(results: list[DownloadResult]) -> bool:
    return bool(results) and all(result.status in SUCCESS_STATUSES for result in results)


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
