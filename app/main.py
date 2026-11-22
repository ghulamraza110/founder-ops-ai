"""FastAPI entrypoint. Run: uvicorn app.main:app --reload

All LLM calls (Manager extraction, Legal Drafter) go through app/llm.py, which uses Groq while
LLM_PROVIDER=groq. Sign-off (approve/edit) is plain Python in app/state_machine.py.
"""
import json
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal

import openai
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import schemas
from app import state_machine as sm
from app.agents.manager_agent import calculate_83b_deadline
from app.agents.state import StartupTerms, export_dir
from app.models import AuditLog, Document, Founder, StartupCase, get_db, init_db, log_action
from app.workflow import case_key, chat_history, chat_terms, run_chat, run_from_terms


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Startup Incorporation & Compliance Engine", lifespan=lifespan)


# ---------- helpers ----------

def get_case_or_404(db: Session, case_id: int) -> StartupCase:
    case = db.get(StartupCase, case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    return case


def case_to_terms(case: StartupCase) -> StartupTerms:
    entity = {"C-Corp": "Delaware C-Corp"}.get(case.entity_type, case.entity_type)
    return StartupTerms.model_validate({
        "company_name": case.name,
        "entity_type": entity if entity in ("Delaware C-Corp", "LLC") else None,
        "incorporation_date": case.incorporation_date,
        "founders": [
            {"name": f.name, "email": f.email or None, "equity_percent": f.equity_percent,
             "vesting_months": f.vesting_months, "cliff_months": f.cliff_months}
            for f in case.founders
        ],
    })


def case_exports_dir(case: StartupCase) -> Path:
    return export_dir(case_key(case.id))


def list_exports(case: StartupCase) -> list[str]:
    return sorted(p.name for p in case_exports_dir(case).iterdir() if p.is_file())


def doc_out(doc: Document) -> schemas.DocumentOut:
    approvals = sm.get_approvals(doc)
    return schemas.DocumentOut(
        id=doc.id, case_id=doc.case_id, doc_type=doc.doc_type, version=doc.version,
        content_md=doc.content_md, status=sm.status_of(doc).value, approvals=approvals,
        pending=[n for n, v in approvals.items() if v != doc.version],
    )


def run_agents(fn, *args):
    """Turn LLM/provider failures into a readable 502 instead of a bare 500."""
    try:
        return fn(*args)
    except (openai.OpenAIError, RuntimeError) as e:
        raise HTTPException(502, f"LLM error: {e}")


def sign_off(fn, *args):
    """Map state-machine errors to HTTP codes."""
    try:
        return fn(*args)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except sm.SignOffError as e:
        raise HTTPException(400, str(e))


def latest_doc(case: StartupCase, doc_type: str) -> Document | None:
    docs = [d for d in case.documents if d.doc_type == doc_type]
    return max(docs, key=lambda d: d.version) if docs else None


def save_results(db: Session, case: StartupCase, result: dict, sync_from_terms: bool) -> schemas.WorkflowResult:
    """Write graph output back to the database: case fields, founders, documents, audit log."""
    ready = bool(result.get("terms_ready"))
    terms = StartupTerms.model_validate(result["terms"]) if result.get("terms") else None

    if ready and sync_from_terms and terms:
        # Chat is the source of truth: copy the locked terms onto the case.
        case.name = terms.company_name
        case.entity_type = terms.entity_type
        if terms.entity_type == "Delaware C-Corp":
            case.state = "Delaware"
        case.incorporation_date = terms.incorporation_date
        case.founders.clear()
        for f in terms.founders:
            case.founders.append(Founder(
                name=f.name, email=f.email or "", equity_percent=f.equity_percent,
                vesting_months=f.vesting_months, cliff_months=f.cliff_months,
            ))
        db.flush()
        log_action(db, case.id, "manager_agent", "terms_locked",
                   json.dumps({"83b_deadline": result.get("deadline_83b"), "founders": len(terms.founders)}))

    if ready:
        case.status = "awaiting_approval"
    documents = result.get("documents") or {}
    for doc_type, content in documents.items():
        existing = latest_doc(case, doc_type)
        if existing:
            sm.redraft_document(db, existing, content, actor="legal_drafter")
        else:
            sm.create_document(db, case, doc_type, content, actor="legal_drafter")
    if result.get("cap_table"):
        cap = result["cap_table"]
        log_action(db, case.id, "cap_table_builder", "cap_table_built",
                   f"{cap['issued_shares']:,} of {cap['authorized_shares']:,} {cap['unit']} allocated")

    if not ready:
        case.status = "needs_info"
        log_action(db, case.id, "manager_agent", "needs_info", "; ".join(result.get("questions", [])))
    db.commit()

    last = result.get("messages", [])[-1:] if result.get("messages") else []
    return schemas.WorkflowResult(
        case_id=case.id,
        status=case.status,
        terms_ready=ready,
        questions=result.get("questions", []),
        reply=last[0].content if last else None,
        terms=result.get("terms"),
        deadline_83b=result.get("deadline_83b"),
        documents=list(documents),
        export_files=list_exports(case) if ready else [],
    )


# ---------- cases ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/cases", response_model=schemas.CaseOut)
def create_case(payload: schemas.CaseCreate, db: Session = Depends(get_db)):
    case = StartupCase(**payload.model_dump())
    db.add(case)
    db.flush()
    log_action(db, case.id, "user", "case_created", case.name)
    db.commit()
    return case


@app.get("/cases", response_model=list[schemas.CaseOut])
def list_cases(db: Session = Depends(get_db)):
    return db.query(StartupCase).order_by(StartupCase.id.desc()).all()


@app.post("/cases/chat", response_model=schemas.WorkflowResult)
def chat(payload: schemas.ChatIn, db: Session = Depends(get_db)):
    """Send a founder message to the Manager Agent (Groq). Omit case_id to start a new case.

    Once the terms are complete, the Legal Drafter and Cap Table Builder run in parallel.
    """
    if payload.case_id is None:
        case = StartupCase(name="Untitled startup")
        db.add(case)
        db.flush()
        log_action(db, case.id, "founder", "case_created", "started from chat")
    else:
        case = get_case_or_404(db, payload.case_id)
    log_action(db, case.id, "founder", "chat_message", payload.message[:500])
    db.commit()
    result = run_agents(run_chat, case.id, payload.message)
    return save_results(db, case, result, sync_from_terms=True)


@app.get("/cases/{case_id}", response_model=schemas.CaseFull)
def get_case(case_id: int, db: Session = Depends(get_db)):
    """Everything about a case: terms, cap table, documents with signatures, and the audit log."""
    case = get_case_or_404(db, case_id)
    terms = chat_terms(case_id) or (case_to_terms(case).model_dump(mode="json") if case.founders else None)
    inc = (terms or {}).get("incorporation_date")
    cap_path = case_exports_dir(case) / "cap_table.json"
    audit = db.query(AuditLog).filter_by(case_id=case_id).order_by(AuditLog.id.desc()).all()
    docs = sorted(case.documents, key=lambda d: (d.doc_type, -d.version))
    return schemas.CaseFull(
        **schemas.CaseOut.model_validate(case).model_dump(),
        founders=[schemas.FounderOut.model_validate(f) for f in case.founders],
        terms=terms,
        deadline_83b=calculate_83b_deadline(date.fromisoformat(inc)) if inc else None,
        cap_table=json.loads(cap_path.read_text(encoding="utf-8")) if cap_path.exists() else None,
        documents=[doc_out(d) for d in docs],
        audit_logs=[schemas.AuditLogOut.model_validate(a) for a in audit],
    )


@app.get("/cases/{case_id}/chat", response_model=list[schemas.ChatMessage])
def get_chat(case_id: int, db: Session = Depends(get_db)):
    get_case_or_404(db, case_id)
    return chat_history(case_id)


# ---------- form path ----------

@app.post("/cases/{case_id}/founders", response_model=schemas.FounderOut)
def add_founder(case_id: int, payload: schemas.FounderCreate, db: Session = Depends(get_db)):
    case = get_case_or_404(db, case_id)
    if payload.name in sm.founder_names(case):
        raise HTTPException(400, f"A founder named '{payload.name}' already exists")
    founder = Founder(case_id=case_id, **payload.model_dump())
    db.add(founder)
    log_action(db, case_id, "user", "founder_added", f"{founder.name} ({founder.equity_percent}%)")
    db.commit()
    return founder


@app.post("/cases/{case_id}/run", response_model=schemas.WorkflowResult)
def run_case(case_id: int, db: Session = Depends(get_db)):
    """Validate the founders entered by form in Python, then run both builder agents."""
    case = get_case_or_404(db, case_id)
    result = run_agents(run_from_terms, case_id, case_to_terms(case))
    return save_results(db, case, result, sync_from_terms=False)


# ---------- sign-off ----------

@app.post("/documents/{doc_id}/approve", response_model=schemas.DocumentOut)
def approve_document(doc_id: int, payload: schemas.ApprovalIn, db: Session = Depends(get_db)):
    """Founder signs the current version. EXECUTED once every founder has signed it."""
    return doc_out(sign_off(sm.approve, db, doc_id, payload.founder_name))


@app.post("/documents/{doc_id}/edit", response_model=schemas.EditOut)
def edit_document(doc_id: int, payload: schemas.EditIn, db: Session = Depends(get_db)):
    """New version: every other founder's signature is reset and the change is audit-logged."""
    doc, invalidated = sign_off(sm.edit_document, db, doc_id, payload.founder_name, payload.content_md)
    return schemas.EditOut(document=doc_out(doc), invalidated_signatures=invalidated)


# ---------- downloads ----------

CAP_TABLE_FILES = {"xlsx": "cap_table.xlsx", "csv": "cap_table.csv", "json": "cap_table.json",
                   "vesting_csv": "vesting_schedule.csv"}


@app.get("/cases/{case_id}/cap-table/download")
def download_cap_table(case_id: int, format: Literal["xlsx", "csv", "json", "vesting_csv"] = "xlsx",
                       db: Session = Depends(get_db)):
    case = get_case_or_404(db, case_id)
    path = case_exports_dir(case) / CAP_TABLE_FILES[format]
    if not path.exists():
        raise HTTPException(404, "No cap table yet. Finish the chat or run the builders first.")
    slug = case_key(case_id)
    return FileResponse(path, filename=f"{slug}-{path.name}")


@app.get("/cases/{case_id}/exports", response_model=list[str])
def get_exports(case_id: int, db: Session = Depends(get_db)):
    return list_exports(get_case_or_404(db, case_id))


@app.get("/cases/{case_id}/exports/{filename}")
def download_export(case_id: int, filename: str, db: Session = Depends(get_db)):
    case = get_case_or_404(db, case_id)
    if filename not in list_exports(case):  # also blocks path traversal
        raise HTTPException(404, "File not found")
    return FileResponse(case_exports_dir(case) / filename, filename=filename)
