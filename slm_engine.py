"""SLM Powered Regulatory - engine.

This is the notebook's logic (SLM_Powered_Regulatory.ipynb) copied cell by cell, unchanged,
so the web server and the notebook behave identically. Only two things differ:
  * REQ_FILE / DB_PATH are relative to this folder instead of a Windows path.
  * load_requirements() ignores CSV_COLUMNS entries that are not in the CSV, so any uploaded
    CSV falls back to the alias detection instead of failing.
"""
import os, re, json, unicodedata
from pathlib import Path
from collections import Counter
from datetime import date
import pymupdf4llm
import fitz
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

import chromadb
import ollama
import pandas as pd

LLM_MODEL = "llama3:8b"
EMBED_MODEL = "bge-m3"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = str(BASE_DIR / "vector_db")
COMPANY_COL = "company_docs"
ISO_COL = "iso27001"
REQ_FILE = str(BASE_DIR / "data" / "iso-27001-annex-a.csv")

# Explicit mapping for the supplied CSV (avoids confusing Theme with Control ID).
CSV_COLUMNS = {"id": "Control", "title": "Theme", "requirement": "Summary", "evidence": "Evidence"}

MAX_CHARS = 1500
CHUNK_OVERLAP = 180
TOP_K = 8
MIN_RELEVANCE = 0.32
REL_CUTOFF = 0.62
NUM_CTX = 8192
OCR_ENABLED = True       # OCR is attempted only for pages with no extractable text.
OCR_LANG = "eng"         # Add Arabic traineddata and use "eng+ara" for bilingual scans.


def table_to_md(tbl):
    rows = [[c.text.strip().replace("\n", " ") for c in r.cells] for r in tbl.rows]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    md = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(md)


def read_docx(path):
    doc = Document(path)
    out = []
    for block in doc.element.body.iterchildren():
        if block.tag.endswith("}p"):
            p = Paragraph(block, doc)
            text = p.text.strip()
            if not text:
                continue
            style = p.style.name if p.style is not None else ""
            if style.startswith("Heading"):
                level = "".join(ch for ch in style if ch.isdigit()) or "1"
                out.append("#" * min(int(level), 6) + " " + text)
            elif "List" in style:
                out.append("- " + text)
            else:
                out.append(text)
        elif block.tag.endswith("}tbl"):
            out.append(table_to_md(Table(block, doc)))
    return [(0, "\n\n".join(out))]


def _ocr_pdf_page(page):
    """Optional OCR fallback; requires pytesseract, Pillow and a Tesseract language pack."""
    if not OCR_ENABLED:
        return ""
    try:
        import pytesseract
        from PIL import Image
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        import io
        return pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes("png"))), lang=OCR_LANG).strip()
    except Exception as exc:
        print(f"OCR skipped on page {page.number + 1}: {exc}")
        return ""


def read_pdf(path):
    """Extract page-wise Markdown first; fall back to PyMuPDF text and optional OCR per page."""
    try:
        pages = pymupdf4llm.to_markdown(path, page_chunks=True)
        result = [(i, str(pg.get("text", ""))) for i, pg in enumerate(pages, 1)]
    except Exception as exc:
        print(f"Markdown extraction fallback: {exc}")
        result = []
    doc = fitz.open(path)
    if not result or not any(t.strip() for _, t in result):
        result = [(i + 1, page.get_text("text")) for i, page in enumerate(doc)]
        
    fixed = []
    for i, page in enumerate(doc, 1):
        current = result[i - 1][1] if i <= len(result) else ""
        if len(current.strip()) < 20:
            fallback = page.get_text("text").strip()
            current = fallback if len(fallback) > len(current) else current
            if len(current.strip()) < 20:
                current = _ocr_pdf_page(page) or current
        fixed.append((i, current))
    doc.close()
    return fixed


def read_file(path):
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return read_pdf(path)
    if ext == ".docx":
        return read_docx(path)
    raise ValueError(f"Unsupported file type: {ext}")

def clean_text(t):
    t = unicodedata.normalize("NFKC", str(t)).replace("\x00", "")
    t = t.replace("\u00ad", "")
    t = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", t)  # join line-wrapped hyphenation
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r" *\| *", " | ", t)
    t = re.sub(r"\n[ \t]*\n{2,}", "\n\n", t)
    return t.strip()


def remove_repeated_lines(pages):
    """Remove only short, exact repeated page furniture; preserve repeated evidence statements."""
    if len(pages) < 5:
        return pages
    counts = Counter()
    for _, text in pages:
        seen = set()
        for line in text.splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if 8 <= len(line) <= 90 and not line.startswith("|"):
                seen.add(line)
        counts.update(seen)
    repeated = {line for line, count in counts.items() if count / len(pages) >= 0.82}
    if not repeated:
        return pages
    out = []
    for page, text in pages:
        lines = [line for line in text.splitlines()
                 if re.sub(r"\s+", " ", line).strip() not in repeated]
        out.append((page, "\n".join(lines)))
    return out


def split_table(tbl):
    lines = tbl.splitlines()
    if len(lines) <= 2:
        return [tbl]
    head = "\n".join(lines[:2])
    parts, cur = [], []
    for row in lines[2:]:
        candidate = "\n".join(cur + [row])
        if cur and len(head) + len(candidate) > MAX_CHARS:
            parts.append(head + "\n" + "\n".join(cur))
            cur = []
        cur.append(row)
    if cur:
        parts.append(head + "\n" + "\n".join(cur))
    return parts


def split_long(text):
    sentences = re.split(r"(?<=[.!?؟])\s+|\n(?=\S)", text)
    parts, cur = [], ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if cur and len(cur) + len(sentence) + 1 > MAX_CHARS:
            parts.append(cur.strip())
            # Keep a small tail as context, without duplicating an entire paragraph.
            tail = cur[-CHUNK_OVERLAP:].strip()
            cur = (tail + " " if tail else "") + sentence
        else:
            cur = (cur + " " + sentence).strip()
    if cur:
        parts.append(cur.strip())
    return parts


def chunk_pages(pages, source):
    """Heading-aware, page-aware chunks; table headers repeat when a table is split."""
    chunks, heading, buf, buf_page = [], "", "", 0
    def flush():
        nonlocal buf
        if buf.strip():
            prefix = f"[Section: {heading}]\n" if heading else ""
            chunks.append({"text": prefix + buf.strip(), "source": source,
                           "page": buf_page, "heading": heading})
        buf = ""
    for page, text in pages:
        text = clean_text(text)
        for block in re.split(r"\n\s*\n", text):
            block = block.strip()
            if not block:
                continue
            heading_match = re.match(r"^(#{1,6})\s+(.+)$", block)
            if heading_match:
                flush()
                heading = heading_match.group(2).strip()[:180]
                block = block[len(heading_match.group(0)):].strip()
                if not block:
                    continue
            is_table = block.startswith("|")
            if len(block) > MAX_CHARS:
                flush()
                pieces = split_table(block) if is_table else split_long(block)
                for piece in pieces:
                    buf, buf_page = piece, page
                    flush()
                continue
            if buf and len(buf) + len(block) > MAX_CHARS:
                flush()
            if not buf:
                buf_page = page
            buf += block + "\n\n"
    flush()
    return chunks

client = chromadb.PersistentClient(path=DB_PATH)


def get_collection(name):
    return client.get_or_create_collection(name, metadata={"hnsw:space": "cosine"})


def reset_collection(name):
    try:
        client.delete_collection(name)
    except Exception:
        pass


def embed(texts, batch=32):
    vectors = []
    for i in range(0, len(texts), batch):
        vectors += ollama.embed(model=EMBED_MODEL, input=texts[i:i + batch])["embeddings"]
    return vectors


def index_chunks(chunks, collection_name, batch=32):
    col = get_collection(collection_name)
    for start in range(0, len(chunks), batch):
        part = chunks[start:start + batch]
        col.upsert(
            ids=[f"{c['source']}::{start + j}" for j, c in enumerate(part)],
            documents=[c["text"] for c in part],
            embeddings=embed([c["text"] for c in part]),
            metadatas=[{"source": c["source"], "page": c.get("page", 0), "heading": c.get("heading", ""),
                       "location": c.get("location", "")} for c in part],
        )
    return len(chunks)


def ingest_file(path, collection_name=COMPANY_COL):
    """Full ingestion: read -> clean -> chunk -> embed -> store."""
    pages = remove_repeated_lines([(n, clean_text(t)) for n, t in read_file(path)])
    chunks = chunk_pages(pages, os.path.basename(path))
    return index_chunks(chunks, collection_name)



_ALIASES = {
    "id": ["id", "control", "clause", "control id", "controlid", "ref", "reference", "number", "no", "control no", "control number"],
    "requirement": ["requirement", "requirements", "description", "text", "control text", "statement", "details", "objective", "control description", "summary"],
    "title": ["title", "name", "control name", "heading", "topic", "domain", "category", "theme"],
    "evidence": ["evidence", "expected evidence", "audit evidence", "evidence required", "artifacts"],
    "stage": ["stage", "maturity", "level"]
}

def _n(s):
    return re.sub(r"[\s_\-]+", " ", str(s).strip().lower())


def load_requirements(path=None):
    """Load ISO rows while retaining Control, Theme, Stage, Summary and Evidence."""
    path = Path(path or REQ_FILE)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else data.get("requirements", [])
    if path.suffix.lower() != ".csv":
        raise ValueError("Requirements file must be CSV or JSON")
    df = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            df = pd.read_csv(path, sep=None, engine="python", encoding=enc, dtype=str).fillna("")
            break
        except UnicodeDecodeError:
            continue
    if df is None:
        raise ValueError(f"Could not decode requirements CSV: {path}")
    mapping = {k: v for k, v in CSV_COLUMNS.items() if v in df.columns}  # only mapped columns that exist in this CSV
    normalized = {_n(c): c for c in df.columns}
    for key in ("id", "requirement", "title", "evidence", "stage"):
        if key in mapping:
            continue
        for alias in _ALIASES[key]:
            if alias in normalized:
                mapping[key] = normalized[alias]
                break
    missing = [k for k in ("id", "requirement") if k not in mapping]
    if missing:
        raise ValueError(f"Missing required CSV mapping(s) {missing}; columns found: {list(df.columns)}")
    reqs = []
    for row_num, row in df.iterrows():
        rid = str(row.get(mapping.get("id", ""), "")).strip()
        requirement = str(row.get(mapping.get("requirement", ""), "")).strip()
        if not rid or not requirement:
            continue
        theme = str(row.get(mapping.get("title", ""), "")).strip()
        stage = str(row.get(mapping.get("stage", ""), "")).strip()
        evidence = str(row.get(mapping.get("evidence", ""), "")).strip()
        title = " — ".join(x for x in (theme, stage) if x) or rid
        reqs.append({"id": rid, "title": title, "theme": theme, "stage": stage,
                     "requirement": requirement, "evidence": evidence, "source_row": int(row_num) + 2})
    if not reqs:
        raise ValueError("No valid control rows were loaded from the requirements file")
    return reqs


def ensure_iso_kb(force=False):
    col = get_collection(ISO_COL)
    label = f"ISO requirements ({Path(REQ_FILE).name})"
    if force:
        try:
            col.delete(where={"source": label})
        except Exception:
            pass
    elif col.count():
        existing = col.get(where={"source": label}, limit=1)
        if existing.get("ids"):
            return
    reqs = load_requirements()
    chunks = [{"text": f"{r['id']} {r['title']}: {r['requirement']} Expected evidence examples: {r['evidence']}",
               "source": label, "page": 0, "heading": r["id"],
               "location": f"CSV row {r.get('source_row', '?')}"} for r in reqs]
    index_chunks(chunks, ISO_COL)

_STOPWORDS = {"the", "and", "for", "with", "from", "that", "this", "are", "was", "were", "into", "must", "should", "shall", "each", "their", "its", "has", "have", "been", "being", "all", "any", "not", "only", "or", "of", "to", "in", "on", "by", "a", "an"}

def _tokens(text):
    return {x for x in re.findall(r"[a-zA-Z0-9]+(?:[._/-][a-zA-Z0-9]+)*", str(text).lower())
            if len(x) > 1 and x not in _STOPWORDS}

def lexical_score(query, text):
    q, t = _tokens(query), _tokens(text)
    if not q:
        return 0.0
    overlap = len(q & t) / len(q)
    qnums = {x for x in q if any(ch.isdigit() for ch in x)}
    tnums = {x for x in t if any(ch.isdigit() for ch in x)}
    number_match = len(qnums & tnums) / len(qnums) if qnums else 0.0
    return min(1.0, 0.82 * overlap + 0.18 * number_match) if qnums else overlap


def retrieve(query, collection_name, k=TOP_K):
    col = get_collection(collection_name)
    if col.count() == 0:
        return []
    candidate_n = min(max(k * 5, 20), col.count())
    res = col.query(query_embeddings=[embed([query])[0]], n_results=candidate_n)
    hits = []
    for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        sim = max(0.0, 1.0 - float(dist))
        lex = lexical_score(query, doc)
        score = 0.68 * sim + 0.32 * lex
        if sim < MIN_RELEVANCE and lex < 0.12:
            continue
        hits.append({"text": doc, **meta, "semantic": round(sim, 3),
                     "lexical": round(lex, 3), "score": round(score, 3)})
    hits.sort(key=lambda h: h["score"], reverse=True)
    selected = []
    seen = set()
    for hit in hits:
        identity = (hit.get("source"), hit.get("page"), hit.get("heading"))
        if identity in seen:
            continue
        if selected and hit["score"] < selected[0]["score"] * REL_CUTOFF:
            continue
        selected.append(hit)
        seen.add(identity)
        if len(selected) >= k:
            break
    return selected


def build_context(hits, tag):
    return "\n\n".join(
        f"[{tag}{i}] ({h['source']}, {h.get('location') or ('page ' + str(h['page']) if h['page'] else 'location n/a')}, section: {h['heading'] or '-'})\n{h['text']}"
        for i, h in enumerate(hits, 1))

SYSTEM_QA = """You are a strict ISO 27001 compliance assistant.
Answer ONLY from supplied context. Cite each factual claim with [C#] or [I#].
Do not infer implementation from policy wording alone. Preserve dates, counts and scope exactly.
If context does not support an answer, say: Not found in the provided documents.
Always answer in English."""

SYSTEM_JUDGE = """You are an evidence-based ISO/IEC 27001:2022 control assessor.
Assess ONE control using ONLY the supplied company passages. Do not infer facts or implementation.
The control summary defines the outcome to assess; the Evidence field is illustrative evidence guidance, NOT a checklist.
Distinguish documented intent (policy) from operational proof (records, samples, configuration, metrics, completed activities).
Assess scope, coverage, date/currentness, owner, sample/population, exceptions and operational results where available.
Explicitly identify contradictory evidence and do not silently resolve it. Prefer specific, dated operational records over generic statements, but report unresolved conflicts.
Statuses:
compliant: relevant evidence demonstrates the requirement is implemented and operating with adequate coverage; no material unresolved gap is shown.
partial: meaningful implementation exists, but material scope/coverage/operation/assurance gaps or unresolved contradictory evidence remain.
non_compliant: relevant evidence explicitly demonstrates failure, absence, or a practice contrary to the requirement. Do not use this merely because a document is silent.
no_evidence: no retrieved passage substantively addresses the requirement. Similar words alone are not evidence.
A policy alone normally cannot prove operating effectiveness when the requirement concerns operation. A missing record is an evidence gap, not proof that an activity never occurred.
Use only valid passage tags in supporting. Empty supporting is allowed only for no_evidence.
Return JSON only: {"status":"compliant|partial|non_compliant|no_evidence","reasoning":"1-3 fact-based sentences","supporting":["C1"],"gaps":["specific gap"],"contradictions":["specific conflict between sources, or empty"],"recommendation":"specific action"}.
Do not invent quotes. Do not use outside knowledge."""

STATUSES = ["compliant", "partial", "non_compliant", "no_evidence"]
STATUS_LABEL = {"compliant": "Compliant", "partial": "Partially compliant",
                "non_compliant": "Non-compliant", "no_evidence": "No evidence found"}
SCORE = {"compliant": 1.0, "partial": 0.5, "non_compliant": 0.0, "no_evidence": None}


def chat(system, user, as_json=False):
    kwargs = {"format": "json"} if as_json else {}
    resp = ollama.chat(model=LLM_MODEL, messages=[{"role": "system", "content": system},
                                                  {"role": "user", "content": user}],
                       options={"temperature": 0, "num_ctx": NUM_CTX}, **kwargs)
    return resp["message"]["content"]


def ask(question):
    company, iso = retrieve(question, COMPANY_COL), retrieve(question, ISO_COL)
    if not company and not iso:
        return "Not found in the provided documents.", []
    context = build_context(company, "C") + "\n\n" + build_context(iso, "I")
    return chat(SYSTEM_QA, f"CONTEXT:\n{context}\n\nQUESTION: {question}"), company + iso

def best_excerpt(passage, query, max_chars=520):
    """Return a source-grounded sentence/row; never ask the LLM to generate evidence text."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?؟])\s+|\n", passage)
             if len(p.strip()) > 20 and not p.strip().startswith("|---")]
    if not parts:
        return passage[:max_chars]
    ranked = sorted(parts, key=lambda x: lexical_score(query, x), reverse=True)
    return ranked[0][:max_chars]


def _parse_judge_json(raw):
    try:
        data = json.loads(raw)
    except Exception:
        match = re.search(r"\{.*\}", raw, re.S)
        try:
            data = json.loads(match.group(0)) if match else {}
        except Exception:
            data = {}
    return data if isinstance(data, dict) else {}


def check_requirement(req):
    query = f"{req['id']} {req['title']} {req['requirement']} {req.get('evidence', '')}"
    hits = retrieve(query, COMPANY_COL)
    context = build_context(hits, "C") or "(no relevant passage was retrieved)"
    if not hits:
        return {"id": req["id"], "title": req["title"], "requirement": req["requirement"],
                "status": "no_evidence", "reasoning": "No substantively relevant company evidence was retrieved.",
                "evidence": [], "gaps": ["Evidence was not found in the supplied company documents."],
                "recommendation": "Request the relevant policy, operational record, configuration export or owner confirmation."}

    prompt = (f"CONTROL {req['id']} — {req['title']}\nREQUIREMENT: {req['requirement']}\n"
              + (f"EXPECTED EVIDENCE EXAMPLES (not a checklist): {req.get('evidence', '')}\n" if req.get("evidence") else "")
              + f"\nCOMPANY PASSAGES:\n{context}\n\nReturn only the specified JSON. Cite only the C-tags above.")
    data = _parse_judge_json(chat(SYSTEM_JUDGE, prompt, as_json=True))
    status = data.get("status") if data.get("status") in STATUSES else "no_evidence"
    raw_support = data.get("supporting", [])
    if not isinstance(raw_support, list):
        raw_support = []
    cited = []
    for item in raw_support:
        match = re.fullmatch(r"\s*C(\d+)\s*", str(item))
        if match:
            idx = int(match.group(1))
            if 1 <= idx <= len(hits) and idx not in cited:
                cited.append(idx)
    # Citation validation: only cited, retrieved passages are surfaced as verified evidence.
    evidence = []
    for idx in cited:
        hit = hits[idx - 1]
        evidence.append({"ref": f"{hit['source']} p.{hit['page'] or '-'}",
                         "quote": best_excerpt(hit["text"], req["requirement"]),
                         "verified": True, "retrieval_score": hit["score"], "tag": f"C{idx}"})
    reasoning = str(data.get("reasoning", "")).strip()
    contradictions = data.get("contradictions", [])
    if not isinstance(contradictions, list):
        contradictions = [str(contradictions)] if contradictions else []
    contradictions = [str(x).strip() for x in contradictions if str(x).strip()]
    gaps = data.get("gaps", [])
    if not isinstance(gaps, list):
        gaps = [str(gaps)] if gaps else []
    gaps = [str(g).strip() for g in gaps if str(g).strip()]
    recommendation = str(data.get("recommendation", "")).strip()

    # No valid source citation means the LLM judgment cannot be accepted as proven.
    if status != "no_evidence" and not evidence:
        status = "no_evidence"
        reasoning = "The model did not provide a valid citation to a retrieved company passage; the assessment is therefore unsubstantiated."
        gaps = ["No validated supporting passage was cited."]
        recommendation = "Obtain and assess source records that directly address this control."
    if status == "compliant" and gaps:
        status = "partial"  # internally inconsistent output is conservatively downgraded
    if status == "no_evidence":
        evidence = []
        if not gaps:
            gaps = ["No sufficient evidence was identified in the supplied documents."]
    return {"id": req["id"], "title": req["title"], "requirement": req["requirement"],
            "status": status, "reasoning": reasoning or "The model returned no usable assessment reasoning.",
            "evidence": evidence, "gaps": gaps, "contradictions": contradictions, "recommendation": recommendation,
            "retrieved_passages": len(hits), "validated_citations": len(evidence)}


def run_compliance(progress=None, limit=None):
    reqs = load_requirements()
    if limit is not None:
        reqs = reqs[:limit]
    results = []
    for i, req in enumerate(reqs, 1):
        result = check_requirement(req)
        results.append(result)
        if progress:
            progress(i, len(reqs), req)
    return results


def summarize(results):
    counts = Counter(r["status"] for r in results)
    total = len(results)
    assessed = [r for r in results if r["status"] != "no_evidence"]
    # Evidence coverage is reported separately; no-evidence controls are excluded from score denominator.
    score = (sum(SCORE[r["status"]] for r in assessed) / len(assessed) * 100) if assessed else None
    return {"total": total, "assessed": len(assessed), "unassessed": total - len(assessed),
            "coverage": round(len(assessed) / total * 100, 1) if total else 0,
            "score": round(score, 1) if score is not None else None,
            **{s: counts.get(s, 0) for s in STATUSES}}

def build_pdf(results, summary, path, company="Organization"):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
    from xml.sax.saxutils import escape as esc

    ss = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=ss["BodyText"], fontSize=8.5, leading=11, spaceAfter=3)
    def P(value, style=small):
        return Paragraph(esc(str(value)).replace("\n", "<br/>"), style)
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=1.7*cm, rightMargin=1.7*cm,
                            topMargin=1.6*cm, bottomMargin=1.6*cm)
    score_text = f"{summary['score']}%" if summary.get("score") is not None else "Not calculable (no assessed controls)"
    story = [Paragraph("ISO/IEC 27001:2022 Annex A Assessment", ss["Title"]),
             P(f"Organization: {company} | Date: {date.today()}"),
             P(f"Evidence-based score: {score_text} | Assessment coverage: {summary['coverage']}% ({summary['assessed']} of {summary['total']} controls assessed)"),
             P("Scoring method: Compliant = 1.0, Partially compliant = 0.5, Non-compliant = 0.0. No Evidence Found is excluded from the score denominator and reported separately."),
             P("Scope limitation: this result reflects only the supplied documents and retrieved evidence. Confirm control applicability against the organization's Statement of Applicability and risk treatment plan."),
             Spacer(1, 10)]
    rows = [["Status", "Count"]] + [[STATUS_LABEL[s], summary[s]] for s in STATUSES] + [["Total controls", summary["total"]]]
    table = Table(rows, colWidths=[9*cm, 3*cm], repeatRows=1)
    table.setStyle(TableStyle([("GRID", (0,0), (-1,-1), 0.4, colors.grey),
                               ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
                               ("VALIGN", (0,0), (-1,-1), "TOP")] ))
    story += [table, Spacer(1, 12)]
    for status in ["non_compliant", "partial", "no_evidence", "compliant"]:
        items = [r for r in results if r["status"] == status]
        if not items:
            continue
        story.append(Paragraph(f"{STATUS_LABEL[status]} ({len(items)})", ss["Heading2"]))
        for r in items:
            block = [Paragraph(esc(f"{r['id']} — {r['title']}"), ss["Heading4"]),
                     P("Requirement: " + r["requirement"]), P("Assessment: " + r["reasoning"])]
            for e in r.get("evidence", []):
                tag = f" [{e.get('tag')}]" if e.get("tag") else ""
                block.append(P(f"Source{tag}: {e['ref']} | Validation: {'validated citation' if e.get('verified') else 'unverified'} | {e['quote']}"))
            if r.get("gaps"):
                block.append(P("Gaps: " + "; ".join(str(g) for g in r["gaps"])))
            if r.get("contradictions"):
                block.append(P("Contradictions requiring resolution: " + "; ".join(str(x) for x in r["contradictions"])))
            if r.get("recommendation"):
                block.append(P("Recommended action: " + r["recommendation"]))
            block.append(Spacer(1, 7))
            story.append(KeepTogether(block))
    doc.build(story)
    return path
