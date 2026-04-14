"""
Unit tests for pluggable search backends in tools.py.

All HTTP calls and DDGS are mocked — no real network requests are made.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import tools
from tools import (
    BraveBackend,
    DuckDuckGoBackend,
    LangSearchBackend,
    SearchBackend,
    SerpApiBackend,
    WebSearchTool,
    _get_backend,
)


# ── SearchBackend interface ───────────────────────────────────────────────────


def test_search_backend_is_abstract():
    """SearchBackend cannot be instantiated directly."""
    with pytest.raises(TypeError):
        SearchBackend()


def test_all_backends_implement_interface():
    """All concrete backends satisfy the SearchBackend interface."""
    assert issubclass(DuckDuckGoBackend, SearchBackend)
    assert issubclass(BraveBackend, SearchBackend)
    assert issubclass(SerpApiBackend, SearchBackend)
    assert issubclass(LangSearchBackend, SearchBackend)


# ── _get_backend() selection ──────────────────────────────────────────────────


def test_get_backend_default_is_duckduckgo():
    with patch.object(tools, "SEARCH_BACKEND", "duckduckgo"):
        assert isinstance(_get_backend(), DuckDuckGoBackend)


def test_get_backend_brave_with_key():
    with patch.object(tools, "SEARCH_BACKEND", "brave"), \
         patch.object(tools, "BRAVE_API_KEY", "test-brave-key"):
        backend = _get_backend()
        assert isinstance(backend, BraveBackend)


def test_get_backend_brave_falls_back_without_key():
    with patch.object(tools, "SEARCH_BACKEND", "brave"), \
         patch.object(tools, "BRAVE_API_KEY", None):
        backend = _get_backend()
        assert isinstance(backend, DuckDuckGoBackend)


def test_get_backend_serpapi_with_key():
    with patch.object(tools, "SEARCH_BACKEND", "serpapi"), \
         patch.object(tools, "SERPAPI_KEY", "test-serp-key"):
        backend = _get_backend()
        assert isinstance(backend, SerpApiBackend)


def test_get_backend_serpapi_falls_back_without_key():
    with patch.object(tools, "SEARCH_BACKEND", "serpapi"), \
         patch.object(tools, "SERPAPI_KEY", None):
        backend = _get_backend()
        assert isinstance(backend, DuckDuckGoBackend)


def test_get_backend_langsearch_with_key():
    with patch.object(tools, "SEARCH_BACKEND", "langsearch"), \
         patch.object(tools, "LANGSEARCH_API_KEY", "test-lang-key"):
        backend = _get_backend()
        assert isinstance(backend, LangSearchBackend)


def test_get_backend_langsearch_falls_back_without_key():
    with patch.object(tools, "SEARCH_BACKEND", "langsearch"), \
         patch.object(tools, "LANGSEARCH_API_KEY", ""):
        backend = _get_backend()
        assert isinstance(backend, DuckDuckGoBackend)


def test_get_backend_unknown_defaults_to_duckduckgo():
    with patch.object(tools, "SEARCH_BACKEND", "unknown_backend"):
        assert isinstance(_get_backend(), DuckDuckGoBackend)


# ── DuckDuckGoBackend ─────────────────────────────────────────────────────────


def test_duckduckgo_backend_result_format():
    mock_results = [
        {"title": "Result 1", "href": "https://example.com/1", "body": "Snippet 1"},
        {"title": "Result 2", "href": "https://example.com/2", "body": "Snippet 2"},
    ]
    with patch("tools.DDGS") as mock_ddgs_cls:
        mock_ddgs = MagicMock()
        mock_ddgs_cls.return_value.__enter__.return_value = mock_ddgs
        mock_ddgs.text.return_value = mock_results

        backend = DuckDuckGoBackend()
        results = backend.search("test query", 2)

    assert len(results) == 2
    for r in results:
        assert "title" in r
        assert "url" in r
        assert "snippet" in r
    assert results[0]["url"] == "https://example.com/1"
    assert results[0]["title"] == "Result 1"
    assert results[0]["snippet"] == "Snippet 1"


# ── BraveBackend ──────────────────────────────────────────────────────────────


def test_brave_backend_result_format():
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "web": {
            "results": [
                {"title": "Brave Result", "url": "https://brave.com/r", "description": "Brave snippet"},
            ]
        }
    }
    mock_response.raise_for_status = MagicMock()

    with patch("tools.requests.get", return_value=mock_response):
        backend = BraveBackend("fake-key")
        results = backend.search("brave query", 1)

    assert len(results) == 1
    assert results[0]["title"] == "Brave Result"
    assert results[0]["url"] == "https://brave.com/r"
    assert results[0]["snippet"] == "Brave snippet"


def test_brave_backend_sends_correct_headers():
    mock_response = MagicMock()
    mock_response.json.return_value = {"web": {"results": []}}
    mock_response.raise_for_status = MagicMock()

    with patch("tools.requests.get", return_value=mock_response) as mock_get:
        BraveBackend("my-brave-key").search("query", 3)
        call_kwargs = mock_get.call_args
        assert call_kwargs.kwargs["headers"]["X-Subscription-Token"] == "my-brave-key"
        assert call_kwargs.kwargs["params"]["q"] == "query"


# ── SerpApiBackend ────────────────────────────────────────────────────────────


def test_serpapi_backend_result_format():
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "organic_results": [
            {"title": "SerpAPI Result", "link": "https://serp.com/r", "snippet": "SerpAPI snippet"},
        ]
    }
    mock_response.raise_for_status = MagicMock()

    with patch("tools.requests.get", return_value=mock_response):
        backend = SerpApiBackend("fake-key")
        results = backend.search("serp query", 1)

    assert len(results) == 1
    assert results[0]["title"] == "SerpAPI Result"
    assert results[0]["url"] == "https://serp.com/r"
    assert results[0]["snippet"] == "SerpAPI snippet"


def test_serpapi_backend_respects_max_results():
    items = [
        {"title": f"R{i}", "link": f"https://serp.com/{i}", "snippet": f"S{i}"}
        for i in range(10)
    ]
    mock_response = MagicMock()
    mock_response.json.return_value = {"organic_results": items}
    mock_response.raise_for_status = MagicMock()

    with patch("tools.requests.get", return_value=mock_response):
        results = SerpApiBackend("key").search("query", 3)

    assert len(results) == 3


# ── Consistent result format across backends ──────────────────────────────────


REQUIRED_KEYS = {"title", "url", "snippet"}


@pytest.mark.parametrize("backend,mock_target,mock_return", [
    (
        DuckDuckGoBackend(),
        "tools.DDGS",
        None,  # handled specially below
    ),
    (
        BraveBackend("key"),
        "tools.requests.get",
        MagicMock(**{
            "json.return_value": {"web": {"results": [{"title": "T", "url": "U", "description": "S"}]}},
            "raise_for_status": MagicMock(),
        }),
    ),
    (
        SerpApiBackend("key"),
        "tools.requests.get",
        MagicMock(**{
            "json.return_value": {"organic_results": [{"title": "T", "link": "U", "snippet": "S"}]},
            "raise_for_status": MagicMock(),
        }),
    ),
])
def test_result_keys_are_consistent(backend, mock_target, mock_return):
    if isinstance(backend, DuckDuckGoBackend):
        with patch("tools.DDGS") as mock_ddgs_cls:
            mock_ddgs = MagicMock()
            mock_ddgs_cls.return_value.__enter__.return_value = mock_ddgs
            mock_ddgs.text.return_value = [
                {"title": "T", "href": "U", "body": "S"}
            ]
            results = backend.search("q", 1)
    else:
        with patch(mock_target, return_value=mock_return):
            results = backend.search("q", 1)

    assert len(results) == 1
    assert set(results[0].keys()) >= REQUIRED_KEYS


# ── WebSearchTool integration ─────────────────────────────────────────────────


def _make_ddgs_mock(results):
    """Helper: configure DDGS mock to return given results."""
    mock_ddgs_cls = MagicMock()
    mock_ddgs = MagicMock()
    mock_ddgs_cls.return_value.__enter__.return_value = mock_ddgs
    mock_ddgs.text.return_value = results
    return mock_ddgs_cls


def test_web_search_tool_uses_duckduckgo_by_default():
    tool = WebSearchTool()
    ddgs_results = [{"title": "T", "href": "https://ex.com", "body": "S"}]

    with patch.object(tools, "SEARCH_BACKEND", "duckduckgo"), \
         patch("tools.DDGS", _make_ddgs_mock(ddgs_results)):
        output = tool._run("test query", num_results=1)

    assert "test query" in output
    assert "https://ex.com" in output


def test_web_search_tool_cache_is_hit_on_second_call():
    tool = WebSearchTool()
    tools._search_cache.clear()
    ddgs_results = [{"title": "T", "href": "https://cached.com", "body": "S"}]

    with patch.object(tools, "SEARCH_BACKEND", "duckduckgo"), \
         patch("tools.DDGS", _make_ddgs_mock(ddgs_results)) as mock_ddgs:
        tool._run("cache test", num_results=1)
        output2 = tool._run("cache test", num_results=1)

    assert "[Cached]" in output2
    # DDGS should only have been called once (second call hits cache)
    assert mock_ddgs.call_count == 1
