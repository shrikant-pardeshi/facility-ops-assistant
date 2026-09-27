"""
Knowledge-base loader and retriever (SOPs + compliance clauses, one index).

Documents: data/knowledge_base/**/ in four formats, one reader each. Every
reader returns the same thing - metadata plus a list of (heading, text)
sections - and each section becomes a KBChunk:

    .md    YAML front-matter (doc_id, doc_type, title, tags); sections at '## ' headings
    .txt   optional "Title: / Tags: / Doc-ID: / Doc-Type:" header lines; sections at
           '## ' headings or ALL-CAPS heading lines
    .docx  Word: title from document properties or the Title / Heading 1 paragraph,
           tags from Keywords; sections at Heading 2 paragraphs; lists and tables kept
    .pdf   PDF: title / tags from document metadata; headings detected by font size
           (larger or bold short lines); falls back to one section per page

Missing metadata falls back to the file name (doc_id, e.g. "SOP-SEC-001_...")
and the folder (sops/ -> sop, compliance/ -> compliance). A file that cannot be
read is skipped with a logged error; the rest of the knowledge base still loads.

Retrieval is two-stage BM25, chosen after prototyping on the sample KB:

1. Document stage - BM25 over title + tags (weighted x2) + full text picks the
   best document(s). This proved reliable: 11/11 demo questions hit the right
   document; off-topic queries score <= 0.5 vs >= 4.4 for relevant ones.
2. Section stage - BM25 over the chosen document's sections (heading
   weighted x2) orders the sections that are returned.

Single-stage section-level BM25 was rejected: short sections tie with each
other and repeated tags drown the signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

import frontmatter
from rank_bm25 import BM25Okapi

from facility_ops.config import get_settings
from facility_ops.data_access.db import DataUnavailableError
from facility_ops.logging_config import get_logger
from facility_ops.schemas import KBChunk, KBHit

log = get_logger("knowledge_base")

_STOPWORDS = frozenset(
    "a an the of to and or is are be in on at for with by as if from it my our your i we you "
    "how do does what when which who should can could would will please tell me about any "
    "someone keeps during".split()
)

# A second document is included only if it scores at least this fraction of the best.
SECOND_DOC_RATIO = 0.6


def tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS]


@dataclass
class _Doc:
    doc_id: str
    doc_type: str
    title: str
    tags: list[str]
    sections: list[KBChunk]
    section_index: BM25Okapi


SUPPORTED_SUFFIXES = (".md", ".txt", ".docx", ".pdf")
_DOC_ID = re.compile(r"^([A-Z]{2,6}-[A-Z]{2,6}-\d{3})")
_FOLDER_TYPE = {"sops": "sop", "sop": "sop", "compliance": "compliance"}


@dataclass
class _Parsed:
    meta: dict
    sections: list[tuple[str, str]]


def _split_sections(body: str) -> list[tuple[str, str]]:
    """'## ' headings -> (heading, text). Text before the first heading is the preamble."""
    parts = re.split(r"\n(?=## )", "\n" + body.strip())
    sections = []
    for part in parts[1:]:
        heading, _, text = part.strip().partition("\n")
        sections.append((heading.removeprefix("## ").strip(), text.strip()))
    return sections


def _tags(value) -> list[str]:
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    if isinstance(value, str):
        return [t.strip() for t in re.split(r"[,;]", value.strip("[] ")) if t.strip()]
    return []


# --- one reader per format ---------------------------------------------------


def _read_markdown(path: Path) -> _Parsed:
    post = frontmatter.load(path)
    return _Parsed(dict(post.metadata), _split_sections(post.content))


_TXT_HEADER = re.compile(r"^(title|tags|keywords|doc-id|doc_id|doc-type|doc_type)\s*:\s*(.*)$", re.I)
_CAPS_HEADING = re.compile(r"^(?=.*[A-Z]{3})[A-Z0-9][A-Z0-9 &/()',.-]{2,70}$")


def _read_text(path: Path) -> _Parsed:
    raw = path.read_text(encoding="utf-8-sig")
    if raw.lstrip().startswith("---"):  # front-matter works in .txt too
        post = frontmatter.loads(raw)
        return _Parsed(dict(post.metadata), _split_sections(post.content))
    meta: dict = {}
    lines = raw.splitlines()
    i = 0
    while i < len(lines) and (m := _TXT_HEADER.match(lines[i].strip())):
        key = m.group(1).lower().replace("-", "_")
        meta["tags" if key == "keywords" else key] = m.group(2).strip()
        i += 1
    body = "\n".join(lines[i:])
    if re.search(r"^## ", body, re.M):
        return _Parsed(meta, _split_sections(body))
    # ALL-CAPS lines as headings ("IMMEDIATE ACTIONS")
    sections: list[tuple[str, str]] = []
    heading, buf = None, []
    for line in body.splitlines():
        if _CAPS_HEADING.match(line.strip()) and not line.strip().endswith("."):
            if heading is not None:
                sections.append((heading, "\n".join(buf).strip()))
            heading, buf = line.strip().title(), []
        elif heading is not None:
            buf.append(line)
    if heading is not None:
        sections.append((heading, "\n".join(buf).strip()))
    return _Parsed(meta, sections)


def _read_docx(path: Path) -> _Parsed:
    import docx  # python-docx; imported lazily so a missing package only skips .docx files
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(str(path))
    props = document.core_properties
    meta: dict = {}
    if props.title:
        meta["title"] = props.title
    if props.keywords:
        meta["tags"] = props.keywords
    if props.category:
        meta["doc_type"] = props.category.strip().lower()
    if props.subject and _DOC_ID.match(props.subject.strip()):
        meta["doc_id"] = props.subject.strip()

    lines: list[str] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, document)
            text = para.text.strip()
            style = ((para.style.name if para.style is not None else "") or "").lower()
            if not text:
                continue
            if style in ("title", "heading 1"):
                meta.setdefault("title", text)
            elif style.startswith("heading"):
                lines.append(f"\n## {text}")
            elif "list" in style:
                lines.append(f"- {text}")
            else:
                lines.append(text)
        elif tag == "tbl":
            table = Table(child, document)
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                lines.append(" | ".join(cells))
    return _Parsed(meta, _split_sections("\n".join(lines)))


def _read_pdf(path: Path) -> _Parsed:
    from pypdf import PdfReader  # imported lazily so a missing package only skips .pdf files

    reader = PdfReader(str(path))
    info = reader.metadata or {}
    meta: dict = {}
    if info.get("/Title"):
        meta["title"] = str(info["/Title"])
    if info.get("/Keywords"):
        meta["tags"] = str(info["/Keywords"])
    subject = str(info.get("/Subject") or "").strip()
    if subject.lower() in ("sop", "compliance"):
        meta["doc_type"] = subject.lower()

    # Collect text runs with their font size and weight. A run ending in a line break,
    # or a change of font size / weight, starts a new line. (Vertical positions are not
    # used: many PDF writers position text relative to the previous line.)
    runs: list[tuple[int, float, bool, str]] = []  # (page, size, bold, text)
    for page_no, page in enumerate(reader.pages, start=1):

        def visit(text, cm, tm, font_dict, font_size, _page=page_no):
            if not text or not text.strip():
                return
            scale = abs(tm[3]) or 1.0
            base = str((font_dict or {}).get("/BaseFont", ""))
            runs.append((_page, round(float(font_size) * scale, 1), "Bold" in base, text))

        page.extract_text(visitor_text=visit)

    lines: list[tuple[int, float, bool, str]] = []  # (page, size, bold, text)
    ended = True
    for page_no, size, bold, text in runs:
        same_font = bool(lines) and lines[-1][:3] == (page_no, size, bold)
        # Join a run to the previous line if it continues it, or if both are plain body
        # text (wrapped lines of one paragraph). Bold / larger lines stay separate: headings.
        if same_font and (not ended or not bold):
            p_, s_, b_, t_ = lines[-1]
            lines[-1] = (p_, s_, b_, f"{t_} {text}")
        else:
            lines.append((page_no, size, bold, text))
        ended = text.endswith("\n")
    lines = [(p_, s_, b_, " ".join(t_.split())) for p_, s_, b_, t_ in lines if t_.strip()]
    if not lines:
        return _Parsed(meta, [])

    sizes = sorted(s_ for _, s_, _, _ in lines)
    body_size = sizes[len(sizes) // 2]
    biggest = max(sizes)
    sections: list[tuple[str, str]] = []
    heading, buf = None, []
    for i, (page_no, size, bold, text) in enumerate(lines):
        if i == 0 and size >= biggest and size > body_size * 1.15:
            meta.setdefault("title", text)
            continue
        is_heading = len(text) <= 80 and not text.endswith(".") and (size > body_size * 1.15 or bold)
        if is_heading:
            if heading is not None:
                sections.append((heading, "\n".join(buf).strip()))
            heading, buf = text, []
        elif heading is not None:
            buf.append(text)
    if heading is not None:
        sections.append((heading, "\n".join(buf).strip()))
    if not sections:  # no detectable headings: one section per page
        pages: dict[int, list[str]] = {}
        for page_no, _, _, text in lines:
            pages.setdefault(page_no, []).append(text)
        sections = [(f"Page {n}", "\n".join(t)) for n, t in pages.items()]
    return _Parsed(meta, sections)


_READERS = {".md": _read_markdown, ".txt": _read_text, ".docx": _read_docx, ".pdf": _read_pdf}


def _complete_meta(path: Path, meta: dict, kb_dir: Path) -> dict:
    """Fill gaps from the file name and folder."""
    meta = dict(meta)
    if not meta.get("doc_id") and (m := _DOC_ID.match(path.stem)):
        meta["doc_id"] = m.group(1)
    if not meta.get("doc_type"):
        for part in path.relative_to(kb_dir).parts[:-1]:
            if part.lower() in _FOLDER_TYPE:
                meta["doc_type"] = _FOLDER_TYPE[part.lower()]
    if not meta.get("title"):
        meta["title"] = path.stem.split("_", 1)[-1].replace("_", " ").strip().capitalize()
    return meta


def load_documents(kb_dir: Path) -> list[_Doc]:
    if not kb_dir.exists():
        raise DataUnavailableError(f"Knowledge base folder not found: {kb_dir}")
    docs: list[_Doc] = []
    seen: set[str] = set()
    files = sorted(p for p in kb_dir.rglob("*") if p.suffix.lower() in SUPPORTED_SUFFIXES and p.is_file())
    for path in files:
        if path.name.startswith(("~$", ".")):  # Office lock files, hidden files
            continue
        try:
            parsed = _READERS[path.suffix.lower()](path)
        except ImportError as exc:
            log.error("Skipping %s: a reader library is not installed (%s). Run pip install -r requirements.txt",
                      path.name, exc)
            continue
        except Exception as exc:  # corrupt file, bad YAML... - skip the file, keep the app running
            log.error("Skipping %s: cannot read it (%s)", path.name, exc)
            continue
        meta = _complete_meta(path, parsed.meta, kb_dir)
        if not {"doc_id", "doc_type", "title"} <= meta.keys():
            log.error("Skipping %s: needs doc_id, doc_type and title (metadata or file name/folder)", path.name)
            continue
        doc_id, title = str(meta["doc_id"]), str(meta["title"])
        doc_type = str(meta["doc_type"]).lower()
        if doc_type not in ("sop", "compliance"):
            log.error("Skipping %s: doc_type must be 'sop' or 'compliance', got %r", path.name, doc_type)
            continue
        if doc_id in seen:
            log.error("Skipping %s: duplicate doc_id %s", path.name, doc_id)
            continue
        tags = _tags(meta.get("tags"))
        chunks = [
            KBChunk(
                chunk_id=f"{doc_id}#{i}",
                doc_id=doc_id,
                doc_type=cast(Literal["sop", "compliance"], doc_type),
                title=title,
                section=heading,
                text=text,
                tags=tags,
                source_file=path.relative_to(kb_dir).as_posix(),
            )
            for i, (heading, text) in enumerate(parsed.sections, start=1)
            if heading and text
        ]
        if not chunks:
            log.warning("Skipping %s: no sections found", path.name)
            continue
        seen.add(doc_id)
        section_index = BM25Okapi([tokenize(f"{c.section} {c.section} {c.text}") for c in chunks])
        docs.append(_Doc(doc_id, doc_type, title, tags, chunks, section_index))
    if not docs:
        raise DataUnavailableError(f"No valid knowledge-base documents found in {kb_dir}")
    by_format: dict[str, int] = {}
    for d in docs:
        suffix = Path(d.sections[0].source_file).suffix
        by_format[suffix] = by_format.get(suffix, 0) + 1
    log.info("Knowledge base loaded: %d documents (%s), %d sections", len(docs),
             ", ".join(f"{n} {k}" for k, n in sorted(by_format.items())), sum(len(d.sections) for d in docs))
    return docs


class KnowledgeIndex:
    def __init__(self, docs: list[_Doc]):
        self.docs = docs
        corpus = []
        for d in docs:
            meta = f"{d.title} {' '.join(d.tags)}"
            body = " ".join(f"{c.section} {c.text}" for c in d.sections)
            corpus.append(tokenize(f"{meta} {meta} {body}"))
        self._doc_index = BM25Okapi(corpus)

    def search(
        self, query: str, top_k: int = 4, doc_type: str = "any", min_score: float | None = None
    ) -> list[KBHit]:
        """Best sections from the best document (plus a close runner-up).

        Returns [] when nothing scores above `min_score` - the caller reports
        that as "not found" instead of guessing.
        """
        min_score = get_settings().kb_min_score if min_score is None else min_score
        q = tokenize(query)
        if not q:
            return []
        doc_scores = self._doc_index.get_scores(q)
        ranked = sorted(
            (i for i, d in enumerate(self.docs) if doc_type in ("any", d.doc_type)),
            key=lambda i: -doc_scores[i],
        )
        if not ranked or doc_scores[ranked[0]] < min_score:
            return []

        best = doc_scores[ranked[0]]
        chosen = [ranked[0]]
        if len(ranked) > 1 and doc_scores[ranked[1]] >= max(min_score, SECOND_DOC_RATIO * best):
            chosen.append(ranked[1])

        per_doc = max(1, top_k // len(chosen))
        hits: list[KBHit] = []
        for i in chosen:
            doc = self.docs[i]
            sec_scores = doc.section_index.get_scores(q)
            order = sorted(range(len(doc.sections)), key=lambda j: -sec_scores[j])[:per_doc]
            # Report the document score (what decided relevance); keep document order of sections.
            for j in sorted(order):
                hits.append(KBHit(chunk=doc.sections[j], score=round(float(doc_scores[i]), 2)))
        return hits


@lru_cache(maxsize=4)
def _cached_index(kb_dir: str) -> KnowledgeIndex:
    return KnowledgeIndex(load_documents(Path(kb_dir)))


def get_index() -> KnowledgeIndex:
    """Process-wide cached index (built on first use)."""
    return _cached_index(str(get_settings().kb_dir))
