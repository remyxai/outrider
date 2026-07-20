"""Tests for the arxiv-fallback path:

  - `_fetch_arxiv_asset(arxiv_id)` fetches metadata directly from
    export.arxiv.org and shapes it as an asset envelope
  - Returns None on 404 / malformed XML / empty input / withdrawn entries
  - Never raises (network / parse errors collapse to None)
  - Skip site at §2 tries the fallback on catalog miss before giving up
  - New status name `skipped_pin_arxiv_unresolvable` fires only when
    BOTH catalog and arxiv miss

The motivating case: TIPSv2 (arxiv 2604.12012) published early July 2026,
merged into transformers 2026-07-07 but not yet ingested by the Remyx
catalog when a pin-arxiv dispatch tried to target it.

Run with: pytest tests/test_arxiv_fallback.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402


_TIPSV2_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2604.12012v1</id>
    <title>TIPSv2: Advancing Vision-Language Pretraining with Enhanced
      Patch-Text Alignment</title>
    <summary>We introduce TIPSv2, a text-image encoder family that extends
      dense patch supervision via an iBOT++ objective ...</summary>
    <published>2026-07-15T00:00:00Z</published>
  </entry>
</feed>"""

_EMPTY_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <opensearch:totalResults xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">0</opensearch:totalResults>
</feed>"""

_WITHDRAWN_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/9999.99999</id>
    <title></title>
    <summary></summary>
  </entry>
</feed>"""


class _FakeResp:
    def __init__(self, body: str):
        self._body = body.encode()
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, resp_or_exc):
    def fake(url, timeout=15):
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        return _FakeResp(resp_or_exc)
    monkeypatch.setattr(run.urllib.request, "urlopen", fake)


# ─── _fetch_arxiv_asset ──────────────────────────────────────────────────


def test_fetch_returns_asset_envelope_on_success(monkeypatch):
    _patch_urlopen(monkeypatch, _TIPSV2_ATOM)
    out = run._fetch_arxiv_asset("2604.12012")
    assert out is not None
    assert out["arxiv_id"] == "2604.12012v1"  # canonical form from id URL
    assert "TIPSv2" in out["title"]
    assert "iBOT" in out["abstract"]


def test_fetch_returns_none_on_empty_entries(monkeypatch):
    _patch_urlopen(monkeypatch, _EMPTY_ATOM)
    assert run._fetch_arxiv_asset("9999.99999") is None


def test_fetch_returns_none_on_withdrawn_entry(monkeypatch):
    """Entries with empty title (withdrawn / placeholder) treated as miss."""
    _patch_urlopen(monkeypatch, _WITHDRAWN_ATOM)
    assert run._fetch_arxiv_asset("9999.99999") is None


def test_fetch_returns_none_on_network_error(monkeypatch):
    _patch_urlopen(monkeypatch, ConnectionError("simulated socket reset"))
    assert run._fetch_arxiv_asset("2604.12012") is None


def test_fetch_returns_none_on_malformed_xml(monkeypatch):
    _patch_urlopen(monkeypatch, "not xml at all <<< malformed >>>")
    assert run._fetch_arxiv_asset("2604.12012") is None


def test_fetch_returns_none_on_empty_input():
    assert run._fetch_arxiv_asset("") is None
    assert run._fetch_arxiv_asset("   ") is None
    assert run._fetch_arxiv_asset(None) is None


def test_fetch_never_raises_even_on_wild_exceptions(monkeypatch):
    """A fallback path must not itself become a new skip cause."""
    _patch_urlopen(monkeypatch, RuntimeError("something unexpected"))
    # Should return None, not propagate
    assert run._fetch_arxiv_asset("2604.12012") is None


def test_fetch_canonical_id_falls_back_to_input(monkeypatch):
    """If the entry <id> URL doesn't match the id regex, use the input as-is."""
    atom_without_matchable_id = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>weird-non-standard-id</id>
        <title>Some Paper</title>
        <summary>...</summary>
      </entry>
    </feed>"""
    _patch_urlopen(monkeypatch, atom_without_matchable_id)
    out = run._fetch_arxiv_asset("2604.12012")
    assert out is not None
    assert out["arxiv_id"] == "2604.12012"  # falls back to input
    assert out["title"] == "Some Paper"


def test_fetch_retries_on_429_and_succeeds(monkeypatch):
    """arxiv throttles GH Actions runner IPs — first 429 must retry, not
    surface as a hard miss."""
    import urllib.error
    call_count = {"n": 0}
    def fake_urlopen(req, timeout=20):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise urllib.error.HTTPError(
                url=getattr(req, "full_url", ""), code=429,
                msg="Too Many Requests", hdrs=None, fp=None,
            )
        return _FakeResp(_TIPSV2_ATOM)
    monkeypatch.setattr(run.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)  # skip backoff wait
    out = run._fetch_arxiv_asset("2604.12012")
    assert out is not None
    assert "TIPSv2" in out["title"]
    assert call_count["n"] == 2  # one 429, one success


def test_fetch_gives_up_after_repeated_429(monkeypatch):
    """After 3 attempts still hitting 429 → return None cleanly."""
    import urllib.error
    def fake_urlopen(req, timeout=20):
        raise urllib.error.HTTPError(
            url=getattr(req, "full_url", ""), code=429,
            msg="Too Many Requests", hdrs=None, fp=None,
        )
    monkeypatch.setattr(run.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    assert run._fetch_arxiv_asset("2604.12012") is None


def test_fetch_asset_shape_compatible_with_asset_to_recommendation(monkeypatch):
    """The synthesized envelope must survive `_asset_to_recommendation`
    without KeyError — that's the whole point of matching shape."""
    _patch_urlopen(monkeypatch, _TIPSV2_ATOM)
    out = run._fetch_arxiv_asset("2604.12012")
    rec = run._asset_to_recommendation(
        out, refine_query="pin-arxiv:2604.12012",
        fallback_interest_name="(pin-arxiv)",
        interest_context="",
        experiment_history="",
    )
    assert rec.arxiv_id == "2604.12012v1"
    assert "TIPSv2" in rec.paper_title
    assert rec.interest_context == ""  # arxiv metadata carries no corpus enrichment
    assert rec.experiment_history == ""
