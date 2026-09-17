from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import ClassVar

import pytest

from pmc_downloader import cli
from pmc_downloader.cli import ProgressDisplay, parse_file_types, resolve_identifiers
from pmc_downloader.downloader import DownloadResult
from pmc_downloader.identifiers import PMCID, PMID, Identifier


class FakeEntrezClient:
    """Stand in for the Entrez Utilities with a fixed PMID to PMCID mapping."""

    mapping: ClassVar[dict[str, str | None]] = {}
    requested: ClassVar[list[list[str]]] = []

    def __init__(self, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> FakeEntrezClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        pass

    def pmids_to_pmcids(self, pmids: list[str]) -> dict[str, str | None]:
        type(self).requested.append(list(pmids))
        return {pmid: self.mapping.get(pmid) for pmid in pmids}


@pytest.fixture
def fake_entrez(monkeypatch: pytest.MonkeyPatch) -> type[FakeEntrezClient]:
    FakeEntrezClient.mapping = {}
    FakeEntrezClient.requested = []
    monkeypatch.setattr(cli, "EntrezClient", FakeEntrezClient)
    return FakeEntrezClient


def test_parse_file_types_normalizes_dots_case_and_duplicates() -> None:
    assert parse_file_types("PDF, .xml,pdf,TXT,json") == ("pdf", "xml", "txt", "json")


def test_parse_file_types_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match=r"unsupported file type\(s\): docx"):
        parse_file_types("pdf,docx")


def test_resolve_identifiers_skips_pmids_without_a_pmcid(
    fake_entrez: type[FakeEntrezClient],
) -> None:
    fake_entrez.mapping = {"36969844": "PMC10034327", "1": None}

    resolution = resolve_identifiers(
        [
            Identifier(PMCID, "PMC123"),
            Identifier(PMID, "36969844"),
            Identifier(PMID, "1"),
        ],
        email="researcher@example.org",
    )

    assert fake_entrez.requested == [["36969844", "1"]]
    assert resolution.pmcids == ["PMC123", "PMC10034327"]
    assert resolution.converted == [("36969844", "PMC10034327")]
    assert resolution.unresolved == ["1"]


def test_resolve_identifiers_deduplicates_converted_pmcids(
    fake_entrez: type[FakeEntrezClient],
) -> None:
    fake_entrez.mapping = {"36969844": "PMC123"}

    resolution = resolve_identifiers(
        [Identifier(PMCID, "PMC123"), Identifier(PMID, "36969844")], email=None
    )

    assert resolution.pmcids == ["PMC123"]


def test_resolve_identifiers_skips_lookup_without_pmids(
    fake_entrez: type[FakeEntrezClient],
) -> None:
    resolution = resolve_identifiers([Identifier(PMCID, "PMC123")], email=None)

    assert fake_entrez.requested == []
    assert resolution == cli.Resolution(["PMC123"], [], [])


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


class FakeClient:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        pass


def _fake_download_articles(
    _client: FakeClient,
    pmcids: list[str],
    _file_types: tuple[str, ...],
    output_dir: Path,
    on_pmcid_complete: object,
) -> list[DownloadResult]:
    results = [
        DownloadResult(
            pmcid,
            f"{pmcid}.1",
            "pdf",
            "downloaded",
            output_dir / f"{pmcid}.1.pdf",
            "42 bytes",
        )
        for pmcid in pmcids
    ]
    callback = on_pmcid_complete
    assert callable(callback)
    for result in results:
        callback(result.pmcid, [result])
    return results


def test_main_prints_summary_and_writes_detailed_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
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
    assert "PMIDs" not in captured.err

    [log_path] = tmp_path.glob("pmc-download-*.log")
    log = log_path.read_text(encoding="utf-8")
    assert "[DOWNLOADED] PMC123.1 pdf" in log
    assert "[NOT-FOUND] PMC999: not available" in log
    assert "Run complete: 2 total PMC IDs, 1 succeeded, 1 failed" in log


def test_main_converts_pmids_and_reports_skipped_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_entrez: type[FakeEntrezClient],
) -> None:
    fake_entrez.mapping = {"36969844": "PMC10034327", "1": None}
    monkeypatch.setattr(cli, "PmcClient", FakeClient)
    monkeypatch.setattr(cli, "download_articles", _fake_download_articles)

    exit_code = cli.main(["PMC123,36969844,PMID:1", "--output-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "  Input identifiers: 3\n" in captured.err
    assert "  PMIDs converted to PMCIDs: 1\n" in captured.err
    assert "  PMIDs without a PMCID (skipped): 1\n" in captured.err
    assert "    PMID:1\n" in captured.err
    assert "  Total PMC IDs: 2\n" in captured.err
    assert "  Succeeded: 2\n" in captured.err
    assert "  Failed: 0\n" in captured.err

    [log_path] = tmp_path.glob("pmc-download-*.log")
    log = log_path.read_text(encoding="utf-8")
    assert "Identifiers (3): PMC123, PMID:36969844, PMID:1" in log
    assert "Converted PMID:36969844 to PMC10034327" in log
    assert "[NO-PMCID] PMID:1: skipped" in log
    assert "PMC IDs (2): PMC123, PMC10034327" in log


def test_main_succeeds_quietly_when_every_pmid_converts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_entrez: type[FakeEntrezClient],
) -> None:
    fake_entrez.mapping = {"36969844": "PMC10034327"}
    monkeypatch.setattr(cli, "PmcClient", FakeClient)
    monkeypatch.setattr(cli, "download_articles", _fake_download_articles)

    exit_code = cli.main(["36969844", "--output-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "  PMIDs without a PMCID (skipped): 0\n" in captured.out
    assert "  Total PMC IDs: 1\n" in captured.out


def test_main_reports_all_pmids_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_entrez: type[FakeEntrezClient],
) -> None:
    fake_entrez.mapping = {}
    monkeypatch.setattr(cli, "PmcClient", FakeClient)
    monkeypatch.setattr(cli, "download_articles", _fake_download_articles)

    exit_code = cli.main(["1,2", "--output-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "  PMIDs without a PMCID (skipped): 2\n" in captured.err
    assert "    PMID:1, PMID:2\n" in captured.err
    assert "  Total PMC IDs: 0\n" in captured.err


def test_main_truncates_a_long_skipped_pmid_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_entrez: type[FakeEntrezClient],
) -> None:
    pmids = [str(number) for number in range(1, 26)]
    monkeypatch.setattr(cli, "PmcClient", FakeClient)
    monkeypatch.setattr(cli, "download_articles", _fake_download_articles)

    exit_code = cli.main([",".join(pmids), "--output-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "  PMIDs without a PMCID (skipped): 25\n" in captured.err
    assert f"    {', '.join(f'PMID:{pmid}' for pmid in pmids[:20])}, and 5 more (see log)\n" in (
        captured.err
    )

    [log_path] = tmp_path.glob("pmc-download-*.log")
    log = log_path.read_text(encoding="utf-8")
    assert "[NO-PMCID] PMID:25: skipped" in log
