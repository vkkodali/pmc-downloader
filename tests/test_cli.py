from __future__ import annotations

from pathlib import Path

import pytest

from pmc_downloader.cli import normalize_pmcid, parse_file_types, parse_pmcids, read_pmcids


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("PMC10009416", "PMC10009416"),
        ("pmc10009416", "PMC10009416"),
        ("10009416", "PMC10009416"),
        (" PMC00123 ", "PMC123"),
    ],
)
def test_normalize_pmcid(value: str, expected: str) -> None:
    assert normalize_pmcid(value) == expected


@pytest.mark.parametrize("value", ["", "PMC", "PMID123", "PMC1.2", "PMC0", "-1"])
def test_normalize_pmcid_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="invalid PMC ID"):
        normalize_pmcid(value)


def test_parse_pmcids_normalizes_and_deduplicates() -> None:
    assert parse_pmcids("PMC123, 456,pmc123") == ["PMC123", "PMC456"]


def test_parse_pmcids_rejects_empty_item() -> None:
    with pytest.raises(ValueError, match="empty value"):
        parse_pmcids("PMC123,")


def test_read_pmcids_reads_one_per_line_and_ignores_blanks(tmp_path: Path) -> None:
    source = tmp_path / "ids.txt"
    source.write_text("\ufeffPMC123\n\n456\nPMC123\n", encoding="utf-8")

    assert read_pmcids(source) == ["PMC123", "PMC456"]


def test_read_pmcids_reports_line_number(tmp_path: Path) -> None:
    source = tmp_path / "ids.txt"
    source.write_text("PMC123\nnot-an-id\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"ids\.txt:2: invalid PMC ID"):
        read_pmcids(source)


def test_parse_file_types_normalizes_dots_case_and_duplicates() -> None:
    assert parse_file_types("PDF, .xml,pdf,TXT,json") == ("pdf", "xml", "txt", "json")


def test_parse_file_types_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match=r"unsupported file type\(s\): docx"):
        parse_file_types("pdf,docx")
