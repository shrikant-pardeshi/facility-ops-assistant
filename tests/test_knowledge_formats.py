"""Knowledge base: Markdown, plain text, Word and PDF documents in one index."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from facility_ops.data_access import knowledge_base as kb
from facility_ops.data_access.db import DataUnavailableError

KB_DIR = Path(__file__).resolve().parent.parent / "data" / "knowledge_base"


@pytest.fixture(scope="module")
def docs():
    return {d.doc_id: d for d in kb.load_documents(KB_DIR)}


def test_all_sample_documents_load_in_four_formats(docs):
    assert len(docs) == 20
    formats = {Path(d.sections[0].source_file).suffix for d in docs.values()}
    assert formats == {".md", ".txt", ".docx", ".pdf"}


@pytest.mark.parametrize("doc_id, suffix, doc_type, title, first_section", [
    ("SOP-SEC-001", ".txt", "sop", "Access Card Reader and Door Access Faults", "Who To Contact"),
    ("SOP-HK-001", ".docx", "sop", "Housekeeping - Washrooms, Pantries and Meeting Rooms", "Cleaning schedule"),
    ("REG-FIRE-003", ".pdf", "compliance", "Fire Drills and Evacuation Training", "Illustrative content"),
])
def test_each_format_yields_metadata_and_sections(docs, doc_id, suffix, doc_type, title, first_section):
    d = docs[doc_id]
    assert d.sections[0].source_file.endswith(suffix)
    assert (d.doc_type, d.title) == (doc_type, title)
    assert d.tags, "tags come from the document's own metadata"
    assert d.sections[0].section == first_section
    assert len(d.sections) >= 4 and all(c.text for c in d.sections)


def test_word_tables_and_lists_are_kept(docs):
    text = " ".join(c.text for c in docs["SOP-HK-001"].sections)
    assert "Washrooms | Every 2 hours" in text  # table row
    assert "- Soap, tissue and hand towels" in text  # bullet


def test_pdf_wrapped_lines_join_into_paragraphs(docs):
    drill = next(c for c in docs["REG-FIRE-003"].sections if c.section == "Drill frequency")
    assert "at least twice a year" in drill.text
    assert "not legal text" in docs["REG-FIRE-003"].sections[0].text  # compliance disclaimer


# ---------------------------------------------------------------- robustness --


def test_plain_text_without_header_uses_file_name_and_folder(tmp_path):
    folder = tmp_path / "sops"
    folder.mkdir()
    (folder / "SOP-TST-001_pantry_microwave.txt").write_text(
        "USING THE MICROWAVE\nDo not heat sealed containers.\n\nREPORTING A FAULT\nLog a complaint.\n",
        encoding="utf-8",
    )
    [d] = kb.load_documents(tmp_path)
    assert (d.doc_id, d.doc_type, d.title) == ("SOP-TST-001", "sop", "Pantry microwave")
    assert [c.section for c in d.sections] == ["Using The Microwave", "Reporting A Fault"]


def test_bad_files_are_skipped_not_fatal(tmp_path, caplog):
    folder = tmp_path / "sops"
    folder.mkdir()
    (folder / "SOP-TST-001_ok.txt").write_text("Title: Fine\n\n## Step one\nDo it.\n", encoding="utf-8")
    (folder / "SOP-TST-002_broken.pdf").write_bytes(b"%PDF-1.4 this is not really a pdf")
    (folder / "SOP-TST-003_broken.docx").write_bytes(b"PK not a real word file")
    (folder / "~$SOP-TST-004_lock.docx").write_bytes(b"office lock file")
    (folder / "SOP-TST-001_duplicate.txt").write_text("## Again\nSame id.\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="facility_ops.knowledge_base"):
        docs = kb.load_documents(tmp_path)
    assert [d.doc_id for d in docs] == ["SOP-TST-001"]
    log = caplog.text
    assert "SOP-TST-002_broken.pdf" in log and "SOP-TST-003_broken.docx" in log
    assert "duplicate doc_id" in log and "~$" not in log


def test_missing_reader_library_only_skips_that_format(tmp_path, monkeypatch, caplog):
    folder = tmp_path / "compliance"
    folder.mkdir()
    (folder / "REG-TST-001_rule.txt").write_text("## Rule\nKeep records.\n", encoding="utf-8")
    (folder / "REG-TST-002_rule.pdf").write_bytes(b"%PDF-1.4")

    def no_pypdf(path):
        raise ImportError("No module named 'pypdf'")

    monkeypatch.setitem(kb._READERS, ".pdf", no_pypdf)
    with caplog.at_level(logging.ERROR, logger="facility_ops.knowledge_base"):
        docs = kb.load_documents(tmp_path)
    assert [d.doc_id for d in docs] == ["REG-TST-001"]
    assert "pip install -r requirements.txt" in caplog.text


def test_pdf_without_headings_falls_back_to_pages(tmp_path):
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    folder = tmp_path / "compliance"
    folder.mkdir()
    path = folder / "REG-TST-001_plain.pdf"
    c = canvas.Canvas(str(path))
    c.setFont("Helvetica", 11)
    c.drawString(72, 750, "All lifts need a valid licence from the inspector.")
    c.showPage()
    c.setFont("Helvetica", 11)
    c.drawString(72, 750, "Keep the inspection certificate in the machine room.")
    c.save()
    [d] = kb.load_documents(tmp_path)
    assert [c.section for c in d.sections] == ["Page 1", "Page 2"]


def test_folder_with_no_usable_documents_is_an_error(tmp_path):
    (tmp_path / "notes.csv").write_text("not a knowledge document", encoding="utf-8")
    with pytest.raises(DataUnavailableError):
        kb.load_documents(tmp_path)
