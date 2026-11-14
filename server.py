"""Lex Align backend.

Run:  python server.py            (then open the printed address)
  or: python run.py / run.bat     (starts the server and opens Chrome for you)

Everything the front end calls lives here. The ISO 27001 logic itself is in slm_engine.py, which is the
notebook's code unchanged (Ollama + ChromaDB, hybrid retrieval, auditor prompt, citation validation).
Standard library only for the web layer, so no extra packages are needed beyond requirements.txt.
"""
import argparse
import csv
import html
import io
import json
import mimetypes
import re
import sys
import threading
import traceback
import uuid
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    import slm_engine as E
except ImportError as exc:  # missing third-party package
    print(f"\nA required package is missing: {exc}\nInstall everything with:\n    pip install -r requirements.txt\n")
    sys.exit(1)

BASE = Path(__file__).resolve().parent
FRONT = BASE / "frontend"
DATA = BASE / "app_data"
UPLOADS = DATA / "uploads"
HISTORY = DATA / "history"
STATE_FILE = DATA / "state.json"
for d in (UPLOADS, HISTORY):
    d.mkdir(parents=True, exist_ok=True)

DEFAULT_CSV = Path(E.REQ_FILE)
MAX_UPLOAD = 200 * 1024 * 1024
ID_RE = re.compile(r"^[0-9a-f]{12}$")
VERDICT = {"compliant": "COMPLIANT", "partial": "PARTIAL", "non_compliant": "NOT_COMPLIANT", "no_evidence": "UNKNOWN"}
STATUS_ORDER = ["non_compliant", "partial", "no_evidence", "compliant"]

LOCK = threading.RLock()
APP = {"pdf": None, "csv": None, "iso_ready": False}
JOB = {"run_id": None, "state": "idle", "total": 0, "done": 0, "current": "", "results": [], "error": "", "summary": None}


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def safe_name(name):
    name = Path(str(name)).name
    name = re.sub(r"[^\w.\- ()]+", "_", name, flags=re.UNICODE).strip(" .")
    return name or "upload"


def friendly(exc):
    msg = str(exc)
    low = msg.lower()
    if any(k in low for k in ("connect", "refused", "unreachable", "10061")):
        return "Ollama is not running. Start it with: ollama serve"
    m = re.search(r"model ['\"]?([\w.\-:/]+)['\"]? not found", low)
    if m:
        return f"Model '{m.group(1)}' is not installed in Ollama. Install it with: ollama pull {m.group(1)}"
    return msg or exc.__class__.__name__


def norm_model(n):
    n = str(n).strip()
    return n if ":" in n else n + ":latest"


def list_models():
    """Names of the models installed in Ollama. Raises if Ollama is not reachable."""
    r = E.ollama.list()
    items = r["models"] if isinstance(r, dict) else getattr(r, "models", [])
    names = []
    for m in items:
        n = (m.get("model") or m.get("name")) if isinstance(m, dict) else (getattr(m, "model", None) or getattr(m, "name", None))
        if n:
            names.append(n)
    return names


def require_models(llm=None):
    """Fail early, with a clear message, if Ollama or one of the needed models is missing."""
    try:
        have = {norm_model(n) for n in list_models()}
    except Exception as exc:
        raise ApiError(friendly(exc), 503)
    for name in [x for x in (llm, E.EMBED_MODEL) if x]:
        if norm_model(name) not in have:
            raise ApiError(f"Model '{name}' is not installed in Ollama. Install it with: ollama pull {name}", 400)


def save_state():
    meta = {"pdf": APP["pdf"], "csv": APP["csv"]}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def init_app():
    """Restore what was loaded last time; fall back to the bundled ISO CSV."""
    saved = {}
    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    csv_meta = saved.get("csv")
    if csv_meta and csv_meta.get("file") and (UPLOADS / csv_meta["file"]).is_file():
        E.REQ_FILE = str(UPLOADS / csv_meta["file"])
    elif DEFAULT_CSV.is_file():
        E.REQ_FILE = str(DEFAULT_CSV)
        try:
            csv_meta = {"name": DEFAULT_CSV.name, "file": None, "count": len(E.load_requirements(DEFAULT_CSV))}
        except Exception:
            csv_meta = None
    else:
        csv_meta = None
    APP["csv"] = csv_meta
    pdf_meta = saved.get("pdf")
    try:
        if pdf_meta and E.get_collection(E.COMPANY_COL).count() > 0:
            APP["pdf"] = pdf_meta
    except Exception:
        pass
    save_state()


def prepare_iso():
    """Make sure the ISO collection matches the current requirements CSV (needs the embedding model)."""
    with LOCK:
        if APP["iso_ready"]:
            return
        label = f"ISO requirements ({Path(E.REQ_FILE).name})"
        col = E.get_collection(E.ISO_COL)
        if col.count():
            others = col.get(where={"source": {"$ne": label}}, limit=1)
            if others.get("ids"):
                E.reset_collection(E.ISO_COL)  # a different CSV was indexed before
        E.ensure_iso_kb()
        APP["iso_ready"] = True


# ----------------------------------------------------------------------------------------------
# uploads
# ----------------------------------------------------------------------------------------------
def parse_multipart(headers, body):
    ctype = headers.get("Content-Type", "")
    if "multipart/form-data" not in ctype:
        raise ApiError("Expected a file upload.")
    msg = BytesParser(policy=email_policy).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    files = {}
    for part in msg.iter_parts():
        field = part.get_param("name", header="content-disposition")
        fname = part.get_filename()
        if field and fname is not None:
            files[field] = (fname, part.get_payload(decode=True) or b"")
    return files


def ensure_idle():
    if JOB["state"] == "running":
        raise ApiError("A check is running. Wait for it to finish first.", 409)


def handle_upload_pdf(files):
    ensure_idle()
    if "pdf" not in files:
        raise ApiError("No PDF received.")
    fname, data = files["pdf"]
    if not fname.lower().endswith(".pdf") or not data.startswith(b"%PDF"):
        raise ApiError("Please choose a valid PDF file.")
    require_models(None)
    name = safe_name(fname)
    path = UPLOADS / name
    path.write_bytes(data)
    try:
        # same steps as E.ingest_file(), but we also report pages and chunks
        pages = E.remove_repeated_lines([(n, E.clean_text(t)) for n, t in E.read_file(str(path))])
        chunks = E.chunk_pages(pages, name)
        if not chunks:
            raise ApiError("No readable text found in this PDF. If it is a scan, run OCR on it first (ocrmypdf).")
        with LOCK:
            E.reset_collection(E.COMPANY_COL)
            E.index_chunks(chunks, E.COMPANY_COL)
            APP["pdf"] = {"name": name, "pages": len(pages), "chunks": len(chunks)}
            JOB.update(run_id=None, state="idle", total=0, done=0, current="", results=[], error="", summary=None)
            save_state()
    except ApiError:
        raise
    except Exception as exc:
        traceback.print_exc()
        raise ApiError(friendly(exc), 500)
    return APP["pdf"]


def handle_upload_csv(files):
    ensure_idle()
    if "csv" not in files:
        raise ApiError("No CSV received.")
    fname, data = files["csv"]
    if not fname.lower().endswith(".csv"):
        raise ApiError("Please choose a CSV file.")
    name = safe_name(fname)
    path = UPLOADS / name
    path.write_bytes(data)
    try:
        count = len(E.load_requirements(path))
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise ApiError(f"Could not read this CSV: {exc}")
    with LOCK:
        E.REQ_FILE = str(path)
        APP["csv"] = {"name": name, "file": name, "count": count}
        try:
            E.reset_collection(E.ISO_COL)
        except Exception:
            pass
        APP["iso_ready"] = False
        save_state()
    return {"name": name, "count": count}


# ----------------------------------------------------------------------------------------------
# the compliance run
# ----------------------------------------------------------------------------------------------
def to_row(r):
    """Engine result -> the row shape the front end renders."""
    reason = r.get("reasoning", "")
    if r.get("gaps"):
        reason += "\nGaps: " + "; ".join(r["gaps"])
    if r.get("contradictions"):
        reason += "\nContradictions: " + "; ".join(r["contradictions"])
    ev = r.get("evidence", [])
    pages = []
    for e in ev:
        m = re.search(r"p\.(\d+)", e.get("ref", ""))
        if m and m.group(1) not in pages:
            pages.append(m.group(1))
    return {"ID": r["id"], "Requirement": r["requirement"], "Title": r["title"],
            "Verdict": VERDICT.get(r["status"], "UNKNOWN"), "Reason": reason.strip(),
            "Evidence": "\n\n".join(f"[{e.get('tag', '')}] {e['quote']}" for e in ev),
            "Page": ", ".join(pages), "Recommendation": r.get("recommendation", "")}


def run_job(run_id, model, pdf_meta, csv_meta):
    results, rows = [], []
    try:
        E.LLM_MODEL = model
        prepare_iso()
        reqs = E.load_requirements()
        with LOCK:
            JOB["total"] = len(reqs)
        for i, req in enumerate(reqs, 1):
            with LOCK:
                JOB["current"] = f"{req['id']} {req['title']}"
            res = E.check_requirement(req)
            results.append(res)
            with LOCK:
                rows.append(to_row(res))
                JOB["results"] = list(rows)
                JOB["done"] = i
        summary = E.summarize(results)
        record = {"id": run_id, "created": datetime.now().isoformat(timespec="seconds"), "model": model,
                  "pdf": pdf_meta, "csv": csv_meta, "summary": summary, "results": rows, "raw": results}
        (HISTORY / f"{run_id}.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        with LOCK:
            JOB.update(state="done", current="", summary=summary)
    except Exception as exc:
        traceback.print_exc()
        with LOCK:
            JOB.update(state="error", error=friendly(exc), current="")


def start_run(model):
    ensure_idle()
    if not APP["pdf"]:
        raise ApiError("Please upload the company PDF first.")
    if not APP["csv"]:
        raise ApiError("Please choose the ISO 27001 requirements CSV.")
    model = (model or E.LLM_MODEL).strip() or E.LLM_MODEL
    require_models(model)
    run_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOB.update(run_id=run_id, state="running", total=APP["csv"]["count"], done=0, current="Starting…", results=[], error="", summary=None)
    threading.Thread(target=run_job, args=(run_id, model, dict(APP["pdf"]), dict(APP["csv"])), daemon=True).start()
    return {"run_id": run_id}


def job_status(since):
    with LOCK:
        return {"run_id": JOB["run_id"], "state": JOB["state"], "total": JOB["total"], "done": JOB["done"],
                "current": JOB["current"], "results": JOB["results"][since:], "error": JOB["error"], "summary": JOB["summary"]}


def handle_ask(body):
    question = str(body.get("question", "")).strip()
    if not question:
        raise ApiError("Please type a question.")
    model = str(body.get("model", "")).strip()
    if model and JOB["state"] != "running":
        E.LLM_MODEL = model
    require_models(E.LLM_MODEL)
    try:
        prepare_iso()
        answer, used = E.ask(question)
    except Exception as exc:
        traceback.print_exc()
        raise ApiError(friendly(exc), 500)
    pages = []
    for h in used:
        if not str(h.get("source", "")).startswith("ISO requirements") and h.get("page") and h["page"] not in pages:
            pages.append(h["page"])
    return {"answer": answer, "sources": sorted(pages)}


# ----------------------------------------------------------------------------------------------
# history + reports
# ----------------------------------------------------------------------------------------------
def load_record(run_id):
    if not ID_RE.match(str(run_id or "")):
        raise ApiError("Unknown report.", 404)
    p = HISTORY / f"{run_id}.json"
    if not p.is_file():
        raise ApiError("This report no longer exists.", 404)
    return json.loads(p.read_text(encoding="utf-8"))


def list_history():
    items = []
    for p in HISTORY.glob("*.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            items.append({"id": r["id"], "created": r["created"], "model": r.get("model", ""),
                          "pdf": (r.get("pdf") or {}).get("name", ""), "csv": (r.get("csv") or {}).get("name", ""),
                          "summary": r.get("summary") or {}})
        except Exception:
            continue
    items.sort(key=lambda x: x["created"], reverse=True)
    return items


def delete_record(run_id):
    load_record(run_id)
    for suffix in (".json", ".pdf"):
        (HISTORY / f"{run_id}{suffix}").unlink(missing_ok=True)
    with LOCK:
        if JOB["run_id"] == run_id:
            JOB.update(run_id=None, state="idle", total=0, done=0, current="", results=[], error="", summary=None)
    return {"ok": True}


def pick_record_id(qs):
    rid = (qs.get("id") or [None])[0]
    if rid:
        return rid
    if JOB["state"] == "done" and JOB["run_id"]:
        return JOB["run_id"]
    items = list_history()
    if not items:
        raise ApiError("No report yet. Run a check first.", 404)
    return items[0]["id"]


LABEL = {"compliant": "Compliant", "partial": "Partially compliant", "non_compliant": "Non-compliant", "no_evidence": "No evidence found"}


def report_csv(rec):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ID", "Title", "Status", "Requirement", "Assessment", "Evidence", "Gaps", "Contradictions", "Recommendation"])
    for r in rec["raw"]:
        ev = " | ".join(f"[{e.get('tag', '')}] {e['ref']}: {e['quote']}" for e in r.get("evidence", []))
        w.writerow([r["id"], r["title"], LABEL.get(r["status"], r["status"]), r["requirement"], r["reasoning"], ev,
                    "; ".join(r.get("gaps", [])), "; ".join(r.get("contradictions", [])), r.get("recommendation", "")])
    return ("﻿" + out.getvalue()).encode("utf-8")


def report_html(rec):
    s, esc = rec["summary"], html.escape
    score = f"{s['score']}%" if s.get("score") is not None else "n/a"
    colors = {"compliant": "#1a9a5b", "partial": "#c98a00", "non_compliant": "#d33f49", "no_evidence": "#7a8299"}
    parts = [f"""<!doctype html><html lang="en"><meta charset="utf-8"><title>ISO 27001 report</title>
<style>body{{font:15px/1.6 system-ui,Segoe UI,Arial;margin:0;background:#f4f6fb;color:#1c2333}}main{{max-width:900px;margin:auto;padding:32px 20px}}
.card{{background:#fff;border-radius:12px;padding:16px 20px;margin:12px 0;border-left:5px solid #ccc;box-shadow:0 1px 4px #0001}}
h1{{margin:0 0 4px}}small{{color:#667}}.tag{{font-weight:700;font-size:12px}}pre{{white-space:pre-wrap;font:inherit;background:#f4f6fb;padding:10px 12px;border-radius:8px}}
blockquote{{margin:6px 0;padding:8px 12px;border-left:3px solid #6b7bff;background:#f4f6fb}}</style><main>
<h1>ISO/IEC 27001:2022 Annex A Assessment</h1>
<small>Document: {esc((rec.get('pdf') or {}).get('name', ''))} · Model: {esc(rec.get('model', ''))} · {esc(rec['created'])}</small>
<div class="card"><b>Score: {score}</b> · Coverage {s.get('coverage', 0)}% ({s.get('assessed', 0)} of {s.get('total', 0)} controls assessed)<br>
Compliant {s.get('compliant', 0)} · Partial {s.get('partial', 0)} · Non-compliant {s.get('non_compliant', 0)} · No evidence {s.get('no_evidence', 0)}<br>
<small>Compliant = 1, Partial = 0.5, Non-compliant = 0. “No evidence” is excluded from the score and reported separately. Confirm applicability against your Statement of Applicability.</small></div>"""]
    for st in STATUS_ORDER:
        items = [r for r in rec["raw"] if r["status"] == st]
        if not items:
            continue
        parts.append(f"<h2>{LABEL[st]} ({len(items)})</h2>")
        for r in items:
            b = [f'<div class="card" style="border-left-color:{colors[st]}"><span class="tag" style="color:{colors[st]}">{LABEL[st].upper()}</span> '
                 f'<b>{esc(r["id"])}</b> — {esc(r["title"])}<br>{esc(r["requirement"])}<pre>{esc(r["reasoning"])}</pre>']
            for e in r.get("evidence", []):
                b.append(f'<blockquote><small>[{esc(str(e.get("tag", "")))}] {esc(e["ref"])}</small><br>{esc(e["quote"])}</blockquote>')
            if r.get("gaps"):
                b.append("<p><b>Gaps:</b> " + esc("; ".join(r["gaps"])) + "</p>")
            if r.get("contradictions"):
                b.append("<p><b>Contradictions:</b> " + esc("; ".join(r["contradictions"])) + "</p>")
            if r.get("recommendation"):
                b.append("<p><b>Recommended action:</b> " + esc(r["recommendation"]) + "</p>")
            parts.append("".join(b) + "</div>")
    parts.append("</main></html>")
    return "".join(parts).encode("utf-8")


def report_pdf(rec):
    p = HISTORY / f"{rec['id']}.pdf"
    if not p.is_file():
        try:
            E.build_pdf(rec["raw"], rec["summary"], str(p), company=Path((rec.get("pdf") or {}).get("name", "Organization")).stem)
        except ImportError:
            raise ApiError("PDF export needs reportlab:  pip install reportlab", 501)
    return p.read_bytes()


# ----------------------------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "LexAlign/1.0"

    def log_message(self, fmt, *args):
        if "/api/status" not in (args[0] if args else ""):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_bytes(self, data, ctype, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise ApiError("File is too large.", 413)
        return self.rfile.read(n) if n else b""

    def dispatch(self, method):
        url = urlparse(self.path)
        path, qs = unquote(url.path), parse_qs(url.query)
        try:
            if path.startswith("/api/"):
                return self.api(method, path, qs)
            if method != "GET":
                raise ApiError("Not found.", 404)
            return self.static(path)
        except ApiError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"error": friendly(exc)}, 500)

    def api(self, method, path, qs):
        if method == "GET":
            if path == "/api/state":
                try:
                    models = [m for m in list_models() if not re.search(r"embed|bge|minilm|nomic", m, re.I)]
                    up = True
                except Exception:
                    models, up = [], False
                with LOCK:
                    job = {"state": JOB["state"], "run_id": JOB["run_id"]}
                return self.send_json({"models": models, "ollama": up, "default_model": E.LLM_MODEL,
                                       "pdf": APP["pdf"], "csv": APP["csv"] and {"name": APP["csv"]["name"], "count": APP["csv"]["count"]}, "job": job})
            if path == "/api/status":
                return self.send_json(job_status(int((qs.get("since") or ["0"])[0] or 0)))
            if path == "/api/history":
                return self.send_json({"items": list_history()})
            m = re.fullmatch(r"/api/history/([0-9a-f]{12})", path)
            if m:
                rec = load_record(m.group(1))
                rec.pop("raw", None)
                return self.send_json(rec)
            m = re.fullmatch(r"/api/report\.(html|csv|pdf)", path)
            if m:
                rec = load_record(pick_record_id(qs))
                kind = m.group(1)
                body, ctype = {"html": (report_html, "text/html; charset=utf-8"), "csv": (report_csv, "text/csv; charset=utf-8"),
                               "pdf": (report_pdf, "application/pdf")}[kind]
                return self.send_bytes(body(rec), ctype)
        elif method == "POST":
            if path == "/api/upload":
                return self.send_json(handle_upload_pdf(parse_multipart(self.headers, self.read_body())))
            if path == "/api/upload_csv":
                return self.send_json(handle_upload_csv(parse_multipart(self.headers, self.read_body())))
            if path == "/api/run":
                body = json.loads(self.read_body() or b"{}")
                return self.send_json(start_run(body.get("model")))
            if path == "/api/ask":
                return self.send_json(handle_ask(json.loads(self.read_body() or b"{}")))
            if path == "/api/reset":
                with LOCK:
                    if JOB["state"] != "running":
                        JOB.update(run_id=None, state="idle", total=0, done=0, current="", results=[], error="", summary=None)
                return self.send_json({"ok": True})
        elif method == "DELETE":
            m = re.fullmatch(r"/api/history/([0-9a-f]{12})", path)
            if m:
                return self.send_json(delete_record(m.group(1)))
        raise ApiError("Not found.", 404)

    def static(self, path):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        f = (FRONT / rel).resolve()
        if FRONT.resolve() not in f.parents or not f.is_file():
            raise ApiError("Not found.", 404)
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_bytes(f.read_bytes(), ctype)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_DELETE(self):
        self.dispatch("DELETE")


def main():
    ap = argparse.ArgumentParser(description="Lex Align - ISO 27001 compliance checker")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    init_app()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"\n  Lex Align is running:  http://localhost:{args.port}\n  Press Ctrl+C to stop.\n", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
