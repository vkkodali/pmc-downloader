from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from pmc_downloader import cli
from pmc_downloader.cli import (
    ProgressDisplay,
    normalize_pmcid,
    parse_file_types,
    parse_pmcids,
    read_pmcids,
)
from pmc_downloader.downloader import DownloadResult


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


def test_progress_display_tracks_pmcid_counts_for_redirected_output() -> None:
    output = StringIO()
    progress = ProgressDisplay(2, output)

    progress.start()
    progress.complete(True)
    progress.complete(False)
    progress.close()

    assert output.getvalue().splitlines() == [
        "Total: 2 | Succeeded: 0 | Failed: 0 | Remaining: 2",
        "Total: 2 | Succeeded: 1 | Failed: 0 | Remaining: 1",
        "Total: 2 | Succeeded: 1 | Failed: 1 | Remaining: 0",
    ]


def test_main_prints_summary_and_writes_detailed_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc_info: object) -> None:
            pass

    def fake_download_articles(
        _client: FakeClient,
        pmcids: list[str],
        _file_types: tuple[str, ...],
        output_dir: Path,
        on_pmcid_complete: object,
    ) -> list[DownloadResult]:
        first = DownloadResult(
            pmcids[0],
            f"{pmcids[0]}.1",
            "pdf",
            "downloaded",
            output_dir / f"{pmcids[0]}.1.pdf",
            "42 bytes",
        )
        second = DownloadResult(pmcids[1], None, None, "not-found", detail="not available")
        callback = on_pmcid_complete
        assert callable(callback)
        callback(pmcids[0], [first])
        callback(pmcids[1], [second])
        return [first, second]

    monkeypatch.setattr(cli, "PmcClient", FakeClient)
    monkeypatch.setattr(cli, "download_articles", fake_download_articles)

    exit_code = cli.main(["PMC123,PMC999", "--output-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "Total: 2 | Succeeded: 1 | Failed: 1 | Remaining: 0" in captured.err
    assert "Summary:\n" in captured.err
    assert "  Total PMC IDs: 2\n" in captured.err
    assert "  Succeeded: 1\n" in captured.err
    assert "  Failed: 1\n" in captured.err
    assert "  Remaining: 0\n" in captured.err

    [log_path] = tmp_path.glob("pmc-download-*.log")
    log = log_path.read_text(encoding="utf-8")
    assert "[DOWNLOADED] PMC123.1 pdf" in log
    assert "[NOT-FOUND] PMC999: not available" in log
    assert "Run complete: 2 total PMC IDs, 1 succeeded, 1 failed" in log
