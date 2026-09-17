"""PMID to PMCID conversion through the NCBI Entrez Utilities."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence

from pmc_downloader.http import HttpSession, NcbiRequestError

ELINK_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
LINK_NAME = "pubmed_pmc"
TOOL_NAME = "pmc-downloader"
BATCH_SIZE = 200
LOGGER = logging.getLogger(__name__)


class EntrezError(NcbiRequestError):
    """Raised when an Entrez Utilities lookup cannot be completed."""


class EntrezClient(HttpSession):
    """Look up the PMC copies of PubMed records with ELink."""

    service = "Entrez Utilities"
    error = EntrezError

    def pmids_to_pmcids(self, pmids: Sequence[str]) -> dict[str, str | None]:
        """Map every PMID to its PMCID, or to ``None`` when PMC holds no copy."""
        resolved: dict[str, str | None] = {}
        for batch in _batched(pmids, BATCH_SIZE):
            resolved.update(self._resolve_batch(batch))
        return resolved

    def _resolve_batch(self, pmids: Sequence[str]) -> dict[str, str | None]:
        params = [
            ("dbfrom", "pubmed"),
            ("db", "pmc"),
            ("linkname", LINK_NAME),
            ("retmode", "json"),
            ("tool", TOOL_NAME),
        ]
        if self._email:
            params.append(("email", self._email))
        params.extend(("id", pmid) for pmid in pmids)

        response = self._get(ELINK_URL, params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise EntrezError("Entrez Utilities returned an invalid ELink response") from exc
        if not isinstance(payload, dict):
            raise EntrezError("Entrez Utilities returned an unexpected ELink response")

        linksets = payload.get("linksets")
        if not isinstance(linksets, list):
            detail = payload.get("ERROR") or payload.get("error") or "no link sets were returned"
            raise EntrezError(f"Entrez Utilities could not convert PMIDs: {detail}")

        resolved: dict[str, str | None] = dict.fromkeys(pmids)
        for linkset in linksets:
            if not isinstance(linkset, dict):
                continue
            pmcid = _linked_pmcid(linkset)
            for value in linkset.get("ids") or []:
                pmid = str(value)
                if pmid in resolved:
                    resolved[pmid] = pmcid
        return resolved


def _linked_pmcid(linkset: dict[str, object]) -> str | None:
    linksetdbs = linkset.get("linksetdbs")
    if not isinstance(linksetdbs, list):
        return None
    for linksetdb in linksetdbs:
        if not isinstance(linksetdb, dict) or linksetdb.get("dbto") != "pmc":
            continue
        links = [str(link) for link in linksetdb.get("links") or []]
        numbers = sorted({int(link) for link in links if link.isdigit()})
        if not numbers:
            continue
        if len(numbers) > 1:
            LOGGER.warning(
                "PubMed links %s to several PMC records (%s); using the first",
                linkset.get("ids"),
                ", ".join(f"PMC{number}" for number in numbers),
            )
        return f"PMC{numbers[0]}"
    return None


def _batched(values: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
