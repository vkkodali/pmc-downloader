from __future__ import annotations

import httpx
import pytest

from pmc_downloader import entrez
from pmc_downloader.entrez import EntrezClient, EntrezError


def test_pmids_to_pmcids_maps_linked_records_and_reports_missing_ones() -> None:
    payload = {
        "linksets": [
            {
                "dbfrom": "pubmed",
                "ids": ["36969844"],
                "linksetdbs": [{"dbto": "pmc", "linkname": "pubmed_pmc", "links": ["10034327"]}],
            },
            {"dbfrom": "pubmed", "ids": ["1"]},
            {"dbfrom": "pubmed", "ids": ["999999999"]},
        ]
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    with EntrezClient(
        delay=0, email="researcher@example.org", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.pmids_to_pmcids(["36969844", "1", "999999999"]) == {
            "36969844": "PMC10034327",
            "1": None,
            "999999999": None,
        }

    [request] = requests
    params = request.url.params
    assert params["dbfrom"] == "pubmed"
    assert params["db"] == "pmc"
    assert params["linkname"] == "pubmed_pmc"
    assert params["email"] == "researcher@example.org"
    assert request.url.params.get_list("id") == ["36969844", "1", "999999999"]


def test_pmids_to_pmcids_batches_large_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entrez, "BATCH_SIZE", 2)
    batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ids = request.url.params.get_list("id")
        batches.append(ids)
        return httpx.Response(
            200,
            json={
                "linksets": [
                    {
                        "ids": [pmid],
                        "linksetdbs": [{"dbto": "pmc", "links": [pmid]}],
                    }
                    for pmid in ids
                ]
            },
        )

    with EntrezClient(delay=0, transport=httpx.MockTransport(handler)) as client:
        resolved = client.pmids_to_pmcids(["1", "2", "3"])

    assert batches == [["1", "2"], ["3"]]
    assert resolved == {"1": "PMC1", "2": "PMC2", "3": "PMC3"}


def test_pmids_to_pmcids_ignores_non_pmc_link_sets() -> None:
    payload = {
        "linksets": [
            {
                "ids": ["7"],
                "linksetdbs": [{"dbto": "pubmed", "links": ["8"]}],
            }
        ]
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))

    with EntrezClient(delay=0, transport=transport) as client:
        assert client.pmids_to_pmcids(["7"]) == {"7": None}


def test_pmids_to_pmcids_raises_when_elink_reports_an_error() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ERROR": "Empty id list"})
    )

    with (
        EntrezClient(delay=0, transport=transport) as client,
        pytest.raises(EntrezError, match="Empty id list"),
    ):
        client.pmids_to_pmcids(["1"])


def test_pmids_to_pmcids_raises_on_invalid_json() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json"))

    with (
        EntrezClient(delay=0, transport=transport) as client,
        pytest.raises(EntrezError, match="invalid ELink response"),
    ):
        client.pmids_to_pmcids(["1"])
