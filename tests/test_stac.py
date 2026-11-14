"""Exercise API requests, retry policy and pagination without network access."""

import gzip
import time
from typing import Any

import httpx
import pytest
from test_analysis import args

from earth_observation_worker import analysis


def item(identity: str) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": identity,
        "collection": "sentinel-2-l2a",
        "properties": {"datetime": "2026-06-01T00:00:00Z"},
        "geometry": None,
        "assets": {"red": {"href": "./red.tif"}},
    }


def test_post_search_retry_pagination_and_asset_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    sleeps: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        if request.url.path == "/":
            return httpx.Response(200, json={"links": [{"rel": "search", "href": "/search", "method": "POST"}]})
        if request.url.path == "/search":
            assert request.method == "POST"
            assert b'"collections":["sentinel-2-l2a"]' in request.content
            return httpx.Response(200, json={"features": [item("a")], "links": [{"rel": "next", "href": "/page2"}]})
        return httpx.Response(200, json={"features": [item("b")], "links": []})

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    monkeypatch.setattr(time, "sleep", sleeps.append)
    result = analysis.read_scenes(args(catalog="https://stac.invalid/"), (-80.0, 30.0, -70.0, 40.0))
    assert [row["id"] for row in result] == ["a", "b"]
    assert result[1]["assets"]["red"]["href"] == "https://stac.invalid/red.tif"
    assert sleeps == [0.0]
    assert len(seen) == 4


def test_get_search_and_pagination_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, json={"links": [{"rel": "search", "href": "/search"}]})
        assert request.method == "GET"
        assert request.url.params["collections"] == "sentinel-2-l2a"
        assert request.url.params["bbox"] == "-80.0,30.0,-70.0,40.0"
        return httpx.Response(200, json={"features": [item("a")], "links": [{"rel": "next", "href": str(request.url)}]})

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    with pytest.raises(ValueError, match="cycle"):
        analysis.read_scenes(args(catalog="https://stac.invalid/"), (-80.0, 30.0, -70.0, 40.0))


def test_http_failures_and_long_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    for status, headers, exception in [(401, {}, httpx.HTTPStatusError), (429, {"Retry-After": "300"}, ValueError)]:
        with (
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda request, code=status, hdrs=headers: httpx.Response(code, headers=hdrs)
                )
            ) as client,
            pytest.raises(exception),
        ):
            analysis.request_page(client, "GET", "https://stac.invalid/", None)


def test_network_retries_response_budget_and_malformed_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    sleeps: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("read timed out", request=request)
        if calls == 2:
            return httpx.Response(503, headers={"Retry-After": "invalid date"})
        return httpx.Response(200, content=b"x" * 1025)

    monkeypatch.setattr(time, "sleep", sleeps.append)
    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        pytest.raises(ValueError, match="max_json_bytes"),
    ):
        analysis.request_page(client, "GET", "https://stac.invalid/", None, max_bytes=1024)
    assert calls == 3
    assert sleeps == [0.5, 1.0]


def test_compressed_response_is_decoded_once() -> None:
    payload = b'{"features":[],"links":[]}'
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=gzip.compress(payload), headers={"Content-Encoding": "gzip"})
        )
    ) as client:
        response = analysis.request_page(client, "GET", "https://stac.invalid/", None)
    assert response.json() == {"features": [], "links": []}
    assert "content-encoding" not in response.headers
