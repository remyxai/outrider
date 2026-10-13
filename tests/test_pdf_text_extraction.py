"""PDF body-text extraction behind the arxiv license retry (#128).

The action used to ``apt-get install poppler-utils`` for ``pdftotext``.
That step died on a stale runner package index and failed every run
before the agent started. Extraction now runs on ``pypdf`` (pip, pure
Python) with ``pdftotext`` as an opportunistic fast path when it is
already on PATH. These tests pin the contract: text comes out whichever
extractor is present, and a missing extractor degrades to ``""`` rather
than raising.
"""
import io
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run  # noqa: E402

pypdf = pytest.importorskip("pypdf")

_URL = "https://github.com/scaleapi/SWE-Interact"


def _make_pdf(text: str) -> bytes:
    """A real one-page PDF with ``text`` drawn in Helvetica, so the
    assertions exercise genuine parsing rather than a stub."""
    from pypdf.generic import (
        ArrayObject, DecodedStreamObject, DictionaryObject, NameObject,
    )
    w = pypdf.PdfWriter()
    page = w.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = w._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
    })
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = w._add_object(stream)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.fixture
def no_pdftotext(monkeypatch):
    monkeypatch.setattr(run.shutil, "which", lambda name: None)


def test_pypdf_extracts_github_url_without_pdftotext(no_pdftotext):
    text = run._pdf_bytes_to_text(_make_pdf(f"Code at {_URL} ."))
    assert _URL in text


def test_pdf_slugs_flow_end_to_end_on_pure_python_path(no_pdftotext, monkeypatch):
    """The slug extractor on top of the pure-Python path yields the slug
    the license retry needs — the actual CI shape now that poppler is
    not installed."""
    run._ARXIV_PDF_CACHE.clear()
    pdf = _make_pdf(f"We release code at {_URL} and data elsewhere.")

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(run.urllib.request, "urlopen", lambda req, timeout=0: _Resp(pdf))
    assert run._fetch_arxiv_pdf_github_slugs("2606.30573v1") == ["scaleapi/SWE-Interact"]


def test_pdftotext_preferred_when_on_path(monkeypatch):
    """When poppler happens to be present it wins, and pypdf is not
    consulted — same behavior the action relied on before #128."""
    monkeypatch.setattr(run.shutil, "which", lambda name: "/usr/bin/pdftotext")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"from poppler " + _URL.encode(), stderr=b"")

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    text = run._pdf_bytes_to_text(b"%PDF-1.4 not really parsed")
    assert calls and calls[0][0] == "pdftotext"
    assert text.startswith("from poppler")


def test_pdftotext_failure_falls_through_to_pypdf(monkeypatch):
    monkeypatch.setattr(run.shutil, "which", lambda name: "/usr/bin/pdftotext")

    def broken(cmd, **kw):
        raise OSError("simulated poppler crash")

    monkeypatch.setattr(run.subprocess, "run", broken)
    text = run._pdf_bytes_to_text(_make_pdf(f"see {_URL}"))
    assert _URL in text


def test_no_extractor_degrades_to_empty_string(no_pdftotext, monkeypatch):
    """Neither binary nor library: the retry path returns "" and the run
    continues. This is the whole point — an optional source must never
    fail the job."""
    monkeypatch.setitem(sys.modules, "pypdf", None)  # makes `import pypdf` raise ImportError
    assert run._pdf_bytes_to_text(_make_pdf("anything")) == ""


def test_garbage_bytes_degrade_to_empty_string(no_pdftotext):
    assert run._pdf_bytes_to_text(b"this is not a pdf") == ""
    assert run._pdf_bytes_to_text(b"") == ""
