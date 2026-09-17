from __future__ import annotations

from pathlib import Path

import pytest

from pmc_downloader.identifiers import (
    PMCID,
    PMID,
    Identifier,
    normalize_identifier,
    parse_identifiers,
    read_identifiers,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("PMC10034327", Identifier(PMCID, "PMC10034327")),
        ("pmc10034327", Identifier(PMCID, "PMC10034327")),
        (" PMC00123 ", Identifier(PMCID, "PMC123")),
        ("PMC:123", Identifier(PMCID, "PMC123")),
        ("36969844", Identifier(PMID, "36969844")),
        ("PMID:36969844", Identifier(PMID, "36969844")),
        ("pmid 36969844", Identifier(PMID, "36969844")),
    ],
)
def test_normalize_identifier(value: str, expected: Identifier) -> None:
    assert normalize_identifier(value) == expected


@pytest.mark.parametrize("value", ["", "PMC", "PMC1.2", "PMC0", "0", "-1", "doi:10.1/x"])
def test_normalize_identifier_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="invalid identifier"):
        normalize_identifier(value)


def test_identifier_label_distinguishes_pmids() -> None:
    assert Identifier(PMCID, "PMC123").label == "PMC123"
    assert Identifier(PMID, "123").label == "PMID:123"


def test_parse_identifiers_normalizes_and_deduplicates() -> None:
    assert parse_identifiers("PMC123, 456,pmc123,PMID:456") == [
        Identifier(PMCID, "PMC123"),
        Identifier(PMID, "456"),
    ]


def test_parse_identifiers_rejects_empty_item() -> None:
    with pytest.raises(ValueError, match="empty value"):
        parse_identifiers("PMC123,")


def test_read_identifiers_reads_one_per_line_and_ignores_blanks(tmp_path: Path) -> None:
    source = tmp_path / "ids.txt"
    source.write_text("﻿PMC123\n\n456\nPMC123\n", encoding="utf-8")

    assert read_identifiers(source) == [
        Identifier(PMCID, "PMC123"),
        Identifier(PMID, "456"),
    ]


def test_read_identifiers_reports_line_number(tmp_path: Path) -> None:
    source = tmp_path / "ids.txt"
    source.write_text("PMC123\nnot-an-id\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"ids\.txt:2: invalid identifier"):
        read_identifiers(source)
