"""Parsing of the article identifiers accepted on the command line."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PMCID = "pmcid"
PMID = "pmid"
PMCID_PATTERN = re.compile(r"PMC[:\s]?0*([0-9]+)", re.IGNORECASE)
PMID_PATTERN = re.compile(r"(?:PMID[:\s]?)?0*([0-9]+)", re.IGNORECASE)


@dataclass(frozen=True)
class Identifier:
    """One requested article, identified by its PMCID or its PMID."""

    kind: str
    value: str

    @property
    def label(self) -> str:
        """Return the identifier as it is shown in progress output and logs."""
        return self.value if self.kind == PMCID else f"PMID:{self.value}"


def normalize_identifier(value: str) -> Identifier:
    """Validate and normalize one PMCID or PMID.

    An identifier carrying the ``PMC`` prefix is a PMC accession ID. Anything
    else, with or without a ``PMID`` prefix, is a PubMed ID that has to be
    converted to a PMCID before the article can be downloaded.
    """
    candidate = " ".join(value.split())
    for pattern, kind in ((PMCID_PATTERN, PMCID), (PMID_PATTERN, PMID)):
        match = pattern.fullmatch(candidate)
        if not match:
            continue
        number = int(match.group(1))
        if number == 0:
            break
        return Identifier(kind, f"PMC{number}" if kind == PMCID else str(number))
    raise ValueError(
        f"invalid identifier: {value!r}; expected a PMCID such as PMC10034327 "
        "or a PMID such as 36969844"
    )


def parse_identifiers(value: str) -> list[Identifier]:
    """Parse a comma-delimited list of identifiers, retaining input order."""
    parts = value.split(",")
    if any(not part.strip() for part in parts):
        raise ValueError("identifier list contains an empty value")
    return deduplicate(normalize_identifier(part) for part in parts)


def read_identifiers(path: Path) -> list[Identifier]:
    """Read one identifier per non-empty line from a UTF-8 text file."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ValueError(f"could not read input file {path}: {exc}") from exc

    identifiers: list[Identifier] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            identifiers.append(normalize_identifier(line))
        except ValueError as exc:
            raise ValueError(f"{path}:{line_number}: {exc}") from exc
    if not identifiers:
        raise ValueError(f"input file contains no identifiers: {path}")
    return deduplicate(identifiers)


def deduplicate(values: Iterable[Identifier]) -> list[Identifier]:
    """Drop repeated identifiers while keeping the order they were given in."""
    return list(dict.fromkeys(values))
