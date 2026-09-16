from __future__ import annotations

import hashlib
from pathlib import Path

import httpx

from pmc_downloader.client import PmcClient
from pmc_downloader.downloader import download_articles


def test_download_articles_uses_latest_version_and_handles_types(tmp_path: Path) -> None:
    pdf = b"%PDF test"
    xml = b"<article />"
    pdf_md5 = hashlib.md5(pdf, usedforsecurity=False).hexdigest()
    xml_md5 = hashlib.md5(xml, usedforsecurity=False).hexdigest()
    metadata = {
        "pmcid": "PMC123",
        "version": 2,
        "pdf_url": f"s3://pmc-oa-opendata/PMC123.2/PMC123.2.pdf?md5={pdf_md5}",
        "xml_url": f"s3://pmc-oa-opendata/PMC123.2/PMC123.2.xml?md5={xml_md5}",
        "text_url": None,
    }
    listing = b"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <CommonPrefixes><Prefix>PMC123.1/</Prefix></CommonPrefixes>
      <CommonPrefixes><Prefix>PMC123.2/</Prefix></CommonPrefixes>
    </ListBucketResult>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, content=listing)
        if request.url.path == "/metadata/PMC123.2.json":
            return httpx.Response(200, json=metadata)
        if request.url.path.endswith(".pdf"):
            return httpx.Response(200, content=pdf)
        if request.url.path.endswith(".xml"):
            return httpx.Response(200, content=xml)
        raise AssertionError(f"unexpected request: {request.url}")

    with PmcClient(delay=0, transport=httpx.MockTransport(handler)) as client:
        results = download_articles(
            client,
            ["PMC123"],
            ("pdf", "xml", "txt", "json"),
            tmp_path,
        )

    assert [result.status for result in results] == [
        "downloaded",
        "downloaded",
        "unavailable",
        "downloaded",
    ]
    assert (tmp_path / "PMC123.2.pdf").read_bytes() == pdf
    assert (tmp_path / "PMC123.2.xml").read_bytes() == xml
    assert (tmp_path / "PMC123.2.json").is_file()
    assert not (tmp_path / "PMC123.1.pdf").exists()


def test_download_articles_reports_unknown_pmcid(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"<ListBucketResult />")
    )

    with PmcClient(delay=0, transport=transport) as client:
        results = download_articles(client, ["PMC999"], ("pdf",), tmp_path)

    assert len(results) == 1
    assert results[0].status == "not-found"
    assert results[0].pmcid == "PMC999"


def test_download_articles_reports_progress_after_each_pmcid(tmp_path: Path) -> None:
    listing = b"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <CommonPrefixes><Prefix>PMC123.1/</Prefix></CommonPrefixes>
    </ListBucketResult>"""
    metadata = {"pdf_url": None}
    completed: list[tuple[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("prefix") == "PMC123.":
            return httpx.Response(200, content=listing)
        if request.url.params.get("prefix") == "PMC999.":
            return httpx.Response(200, content=b"<ListBucketResult />")
        if request.url.path == "/metadata/PMC123.1.json":
            return httpx.Response(200, json=metadata)
        raise AssertionError(f"unexpected request: {request.url}")

    with PmcClient(delay=0, transport=httpx.MockTransport(handler)) as client:
        download_articles(
            client,
            ["PMC123", "PMC999"],
            ("pdf",),
            tmp_path,
            on_pmcid_complete=lambda pmcid, results: completed.append(
                (pmcid, [result.status for result in results])
            ),
        )

    assert completed == [
        ("PMC123", ["unavailable"]),
        ("PMC999", ["not-found"]),
    ]
