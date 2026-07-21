"""Tests for the selection_rejected telemetry plumbing.

The selection-pass produces a per-rejected-candidate list with rich
free-form rejection rationale. Before this work the data was only
visible in the local step summary; it didn't reach the engine-side
``recommendation_runs`` telemetry. This made cross-customer analysis
of rejection patterns (baseline code-availability rate, per-(paper,
customer) verdict patterns, deep-research prioritization) impossible
without manual log scraping.

This module ships:

  1. ``_enrich_selection_rejected`` carries ``license_class`` and
     ``license_compat`` alongside the existing arxiv_id / title /
     reason. These are the empirically load-bearing axis identified
     by cross-portfolio analysis.
  2. ``_compact_selection_rejected_for_telemetry`` produces a wire
     projection: drops ``title`` (engine resolves from arxiv_id),
     truncates ``reason`` to keep payload bounded, caps the list
     defensively.
  3. ``_post_run_telemetry``'s payload includes the compacted list
     under ``selection_rejected``.

The combination makes "rejection rate by license_class across last
N days" a single SQL query rather than a manual analysis pass.

Run with: pytest tests/test_selection_rejected_telemetry.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

import run  # noqa: E402
from run import Recommendation  # noqa: E402


def _rec(
    arxiv_id: str,
    license_class: str = "no-code-link",
    license_compat: float = 0.3,
    title: str | None = None,
) -> Recommendation:
    """Build a minimal Recommendation for rejection-enrichment tests."""
    return Recommendation(
        paper_title=title or f"Paper {arxiv_id}",
        arxiv_id=arxiv_id,
        tier="high",
        z_score=0.0,
        spec_md="",
        paper_abstract="",
        domain_summary="",
        raw_paper_md="",
        relevance_score=0.9,
        license_class=license_class,
        license_compat=license_compat,
    )


# ─── _enrich_selection_rejected: carries license fields ────────────


def test_enrich_carries_license_class_and_compat():
    """The enriched entry must include license_class + license_compat
    from the matched candidate. Without these fields the engine-side
    rejection-rate-by-license-class query is impossible."""
    viable = [
        _rec("2606.00001v1", license_class="no-code-link", license_compat=0.3),
        _rec("2606.00002v1", license_class="permissive", license_compat=1.0),
    ]
    raw = [
        {"index": 0, "why": "no code; semantic-entropy needs multi-sample"},
        {"index": 1, "why": "wrong problem class (video vs static images)"},
    ]
    enriched = run._enrich_selection_rejected(raw, viable)
    assert len(enriched) == 2
    assert enriched[0]["license_class"] == "no-code-link"
    assert enriched[0]["license_compat"] == 0.3
    assert enriched[1]["license_class"] == "permissive"
    assert enriched[1]["license_compat"] == 1.0


def test_enrich_preserves_existing_fields():
    """License-field addition must not regress the existing fields the
    step-summary renderer + downstream consumers depend on."""
    viable = [_rec("2606.00001v1", title="Test Paper Title")]
    enriched = run._enrich_selection_rejected(
        [{"index": 0, "why": "rejected for reason X"}], viable
    )
    assert enriched[0]["arxiv_id"] == "2606.00001v1"
    assert enriched[0]["title"] == "Test Paper Title"
    assert enriched[0]["reason"] == "rejected for reason X"


def test_enrich_out_of_range_index_gets_safe_defaults():
    """Out-of-range index (defensive path when the agent emits a malformed
    selection JSON) should still produce a complete record — unknown
    license_class and zero compat — so downstream consumers don't crash
    on missing keys."""
    enriched = run._enrich_selection_rejected(
        [{"index": 999, "why": "bad index"}],
        viable=[_rec("2606.00001v1")],
    )
    assert enriched[0]["arxiv_id"] == ""
    assert enriched[0]["license_class"] == "unknown"
    assert enriched[0]["license_compat"] == 0.0


def test_enrich_tolerates_bare_string_entries():
    """Looser backends (observed with GLM) emit rejected
    entries as bare rationale strings instead of the
    ``{index, why}`` objects the selection schema specifies. These must
    be carried as unattributed rejections, not crash the run with
    ``'str' object has no attribute 'get'``."""
    viable = [_rec("2606.00001v1")]
    enriched = run._enrich_selection_rejected(
        [
            "paper assumes a trainer the repo lacks",
            {"index": 0, "why": "wrong problem class"},
        ],
        viable,
    )
    assert len(enriched) == 2
    assert enriched[0]["title"] == "(unattributed rejection)"
    assert enriched[0]["reason"] == "paper assumes a trainer the repo lacks"
    assert enriched[0]["license_class"] == "unknown"
    assert enriched[1]["arxiv_id"] == "2606.00001v1"


def test_enrich_drops_non_dict_non_str_entries():
    """Entries that are neither dict nor str (e.g. a stray int or list)
    are dropped rather than crashing — the rejection list only feeds the
    step summary and telemetry."""
    viable = [_rec("2606.00001v1")]
    enriched = run._enrich_selection_rejected(
        [42, ["nested"], {"index": 0, "why": "kept"}], viable
    )
    assert len(enriched) == 1
    assert enriched[0]["reason"] == "kept"


def test_enrich_coerces_non_string_why():
    """A non-string ``why`` (None, dict, …) is coerced to str so the
    telemetry compaction's reason-truncation slice can't crash."""
    viable = [_rec("2606.00001v1")]
    enriched = run._enrich_selection_rejected(
        [{"index": 0, "why": None}, {"index": 0, "why": {"text": "odd"}}],
        viable,
    )
    assert enriched[0]["reason"] == ""
    assert isinstance(enriched[1]["reason"], str)


def test_enrich_empty_raw_returns_empty_list():
    """Empty input → empty list; not None, not error. Downstream
    consumers can iterate without nil-checks."""
    assert run._enrich_selection_rejected([], viable=[_rec("2606.00001v1")]) == []


# ─── _compact_selection_rejected_for_telemetry: wire projection ────


def test_compact_drops_title():
    """Title is omitted from the engine-side wire payload — the engine
    resolves paper metadata from arxiv_id, no need to duplicate per-run
    per-customer."""
    enriched = [
        {
            "arxiv_id": "2606.00001v1",
            "title": "A Long Paper Title That Doesn't Belong in Wire Payload",
            "reason": "rejected because X",
            "license_class": "no-code-link",
            "license_compat": 0.3,
        }
    ]
    compact = run._compact_selection_rejected_for_telemetry(enriched)
    assert "title" not in compact[0]
    assert compact[0]["arxiv_id"] == "2606.00001v1"
    assert compact[0]["license_class"] == "no-code-link"


def test_compact_preserves_arxiv_and_license_fields():
    """The fields that drive engine-side analysis (arxiv_id + license_*)
    must survive the wire compaction unchanged."""
    enriched = [
        {
            "arxiv_id": "2606.99999v1",
            "title": "ignored",
            "reason": "short reason",
            "license_class": "permissive",
            "license_compat": 1.0,
        }
    ]
    compact = run._compact_selection_rejected_for_telemetry(enriched)
    assert compact[0]["arxiv_id"] == "2606.99999v1"
    assert compact[0]["license_class"] == "permissive"
    assert compact[0]["license_compat"] == 1.0
    assert compact[0]["reason"] == "short reason"


def test_compact_truncates_long_reasons():
    """Reasons are truncated to 300 chars to keep payload bounded on
    rich runs (selection-pass rationales can run 500-1000 chars when
    the agent is thorough)."""
    long_reason = "X" * 1000
    enriched = [
        {
            "arxiv_id": "2606.00001v1",
            "title": "",
            "reason": long_reason,
            "license_class": "missing",
            "license_compat": 0.0,
        }
    ]
    compact = run._compact_selection_rejected_for_telemetry(enriched)
    assert len(compact[0]["reason"]) <= 300


def test_compact_caps_list_length():
    """List length is capped defensively at 50. A misbehaving agent
    that emits 100+ rejections shouldn't blow up the engine telemetry
    payload size."""
    enriched = [
        {
            "arxiv_id": f"2606.{i:05d}v1",
            "title": "",
            "reason": "rejection reason",
            "license_class": "unknown",
            "license_compat": 0.5,
        }
        for i in range(80)
    ]
    compact = run._compact_selection_rejected_for_telemetry(
        enriched, max_entries=50
    )
    assert len(compact) == 50


def test_compact_returns_none_for_empty_input():
    """Empty / None input → None return (not []) so the engine-side
    column can distinguish 'rejection list was empty' from 'rejection
    list wasn't shipped by this action version' via NULL vs []."""
    assert run._compact_selection_rejected_for_telemetry(None) is None
    assert run._compact_selection_rejected_for_telemetry([]) is None


def test_compact_no_payload_bloat_on_typical_run():
    """A typical run has 25-35 rejected candidates with ~200-char
    reasons. The compacted payload should stay well under any
    reasonable size budget (engine schema sets the actual ceiling)."""
    import json

    typical_enriched = [
        {
            "arxiv_id": f"2606.{i:05d}v1",
            "title": "A representative paper title that we drop from wire",
            "reason": (
                "Rejected because the paper's I/O contract doesn't match "
                "the call site this verification pass identified — concretely, "
                "the paper assumes X but the target repo expects Y."
            ),
            "license_class": "no-code-link",
            "license_compat": 0.3,
        }
        for i in range(35)
    ]
    compact = run._compact_selection_rejected_for_telemetry(typical_enriched)
    serialized = json.dumps(compact)
    # 35 entries × ~250 chars = ~9KB. Should be well under 50KB.
    assert len(serialized) < 50_000, (
        f"telemetry payload exceeded 50KB sanity ceiling: {len(serialized)} bytes"
    )


# ─── _post_run_telemetry payload includes the new field ────────────


def test_telemetry_payload_includes_selection_rejected(monkeypatch, tmp_path):
    """The engine-side telemetry POST must include the compacted
    rejection list. The whole point of this plumbing is making this data
    queryable on engine; if the payload doesn't carry it, all the
    upstream enrichment is wasted."""
    captured = {}

    def fake_remyx_post(path, payload):
        captured["path"] = path
        captured["payload"] = payload

    monkeypatch.setattr(run, "_remyx_post", fake_remyx_post)
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")

    result = {
        "status": "skipped_by_selection_verification",
        "selection_rejected": [
            {
                "arxiv_id": "2606.00001v1",
                "title": "Rejected Paper",
                "reason": "no call site",
                "license_class": "no-code-link",
                "license_compat": 0.3,
            },
            {
                "arxiv_id": "2606.00002v1",
                "title": "Another Rejected Paper",
                "reason": "wrong problem class",
                "license_class": "permissive",
                "license_compat": 1.0,
            },
        ],
    }
    target = run.Target(repo="owner/repo", interest_id="iid")

    run._post_run_telemetry(result, target)

    payload = captured["payload"]
    assert "selection_rejected" in payload
    assert len(payload["selection_rejected"]) == 2
    # Title dropped at compaction; arxiv_id + license fields preserved.
    assert "title" not in payload["selection_rejected"][0]
    assert payload["selection_rejected"][0]["arxiv_id"] == "2606.00001v1"
    assert payload["selection_rejected"][0]["license_class"] == "no-code-link"
    assert payload["selection_rejected"][1]["license_class"] == "permissive"


def test_telemetry_payload_selection_rejected_null_when_absent(monkeypatch, tmp_path):
    """A run with no rejected candidates (e.g., happy-path PR ship)
    should send NULL, not [], so engine-side reads can distinguish
    'no rejections' from 'old action version that didn't ship the
    field' via the column being null."""
    captured = {}
    monkeypatch.setattr(run, "_remyx_post", lambda p, payload: captured.update(payload=payload))
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")

    result = {"status": "pr_opened_draft"}  # no selection_rejected key
    target = run.Target(repo="owner/repo", interest_id="iid")

    run._post_run_telemetry(result, target)

    assert "selection_rejected" in captured["payload"]
    assert captured["payload"]["selection_rejected"] is None


def test_telemetry_payload_does_not_leak_pii():
    """Sanity: per-rejected entries don't carry anything customer-PII-shaped
    (full paper text, repo internals, env vars). The engine resolves
    everything except the agent's rejection reason from arxiv_id +
    customer identity already on the run record."""
    enriched = [
        {
            "arxiv_id": "2606.00001v1",
            "title": "ignored",
            "reason": "rejection reason text",
            "license_class": "no-code-link",
            "license_compat": 0.3,
        }
    ]
    compact = run._compact_selection_rejected_for_telemetry(enriched)
    # Allowed keys only — defends against a future contributor adding
    # something sensitive (full diff, env-var dump, etc.) to the
    # per-rejected entry.
    allowed_keys = {"arxiv_id", "license_class", "license_compat", "reason"}
    for entry in compact:
        unexpected = set(entry.keys()) - allowed_keys
        assert not unexpected, (
            f"per-rejected entry contains unexpected keys {unexpected} — "
            f"review for PII / secret-shape content before adding"
        )
