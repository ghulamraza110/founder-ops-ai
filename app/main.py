"""FastAPI entrypoint. Run: uvicorn app.main:app --reload"""
import json
from contextlib import asynccontextmanager
from pathlib import Path

import openai
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import schemas
from app.agents.state import StartupTerms, export_dir
from app.models import AuditLog, Document, Founder, StartupCase, get_db, init_db, log_action
from app.state_machine import case_key, chat_history, run_chat, run_from_terms


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


def founder_id(f: Founder) -> str:
    """Approvals are recorded by email, or by name when no email is on file."""
    return f.email or f.name


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


def run_agents(fn, *args):
    """Turn LLM/provider failures into a readable 502 instead of a bare 500."""
    try:
        return fn(*args)
    except (openai.OpenAIError, RuntimeError) as e:
        raise HTTPException(502, f"LLM error: {e}")


def save_results(db: Session, case: StartupCase, result: dict, sync_from_terms: bool) -> schemas.WorkflowResult:
    """Write graph output back to the database: case fields, founders, document versions, audit log."""
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
        log_action(db, case.id, "manager_agent", "terms_locked",
                   json.dumps({"83b_deadline": result.get("deadline_83b"), "founders": len(terms.founders)}))

    documents = result.get("documents") or {}
    for doc_type, content in documents.items():
        latest = (db.query(Document).filter_by(case_id=case.id, doc_type=doc_type)
                  .order_by(Document.version.desc()).first())
        db.add(Document(case_id=case.id, doc_type=doc_type,
                        version=(latest.version + 1) if latest else 1, content_md=content))
        log_action(db, case.id, "legal_drafter", "document_drafted", doc_type)
    if result.get("cap_table"):
        cap = result["cap_table"]
        log_action(db, case.id, "cap_table_builder", "cap_table_built",
                   f"{cap['issued_shares']:,} of {cap['authorized_shares']:,} {cap['unit']} allocated")

    case.status = "awaiting_approval" if ready else "needs_info"
    if not ready:
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


# ---------- cases & founders ----------

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


@app.get("/cases/{case_id}", response_model=schemas.CaseDetail)
def get_case(case_id: int, db: Session = Depends(get_db)):
    return get_case_or_404(db, case_id)


@app.post("/cases/{case_id}/founders", response_model=schemas.FounderOut)
def add_founder(case_id: int, payload: schemas.FounderCreate, db: Session = Depends(get_db)):
    get_case_or_404(db, case_id)
    founder = Founder(case_id=case_id, **payload.model_dump())
    db.add(founder)
    log_action(db, case_id, "user", "founder_added", f"{founder.name} ({founder.equity_percent}%)")
    db.commit()
    return founder


# ---------- agent workflows ----------

@app.post("/cases/{case_id}/chat", response_model=schemas.WorkflowResult)
def chat(case_id: int, payload: schemas.ChatIn, db: Session = Depends(get_db)):
    """Send a founder message to the Manager Agent; builders run automatically once terms are complete."""
    case = get_case_or_404(db, case_id)
    log_action(db, case_id, "founder", "chat_message", payload.message[:500])
    result = run_agents(run_chat, case_id, payload.message)
    return save_results(db, case, result, sync_from_terms=True)


@app.get("/cases/{case_id}/chat", response_model=list[schemas.ChatMessage])
def get_chat(case_id: int, db: Session = Depends(get_db)):
    get_case_or_404(db, case_id)
    return chat_history(case_id)


@app.post("/cases/{case_id}/run", response_model=schemas.WorkflowResult)
def run_case(case_id: int, db: Session = Depends(get_db)):
    """Form path: validate the case's founders in Python, then run both builder agents."""
    case = get_case_or_404(db, case_id)
    result = run_agents(run_from_terms, case_id, case_to_terms(case))
    return save_results(db, case, result, sync_from_terms=False)


# ---------- outputs ----------

@app.get("/cases/{case_id}/cap-table")
def get_cap_table(case_id: int, db: Session = Depends(get_db)):
    path = case_exports_dir(get_case_or_404(db, case_id)) / "cap_table.json"
    if not path.exists():
        raise HTTPException(404, "No cap table yet. Run the workflow first.")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/cases/{case_id}/exports", response_model=list[str])
def get_exports(case_id: int, db: Session = Depends(get_db)):
    return list_exports(get_case_or_404(db, case_id))


@app.get("/cases/{case_id}/exports/{filename}")
def download_export(case_id: int, filename: str, db: Session = Depends(get_db)):
    case = get_case_or_404(db, case_id)
    if filename not in list_exports(case):  # also blocks path traversal
        raise HTTPException(404, "File not found")
    return FileResponse(case_exports_dir(case) / filename, filename=filename)


@app.get("/cases/{case_id}/documents", response_model=list[schemas.DocumentOut])
def list_documents(case_id: int, db: Session = Depends(get_db)):
    get_case_or_404(db, case_id)
    return db.query(Document).filter_by(case_id=case_id).order_by(Document.doc_type, Document.version.desc()).all()


@app.post("/documents/{doc_id}/approve", response_model=schemas.DocumentOut)
def approve_document(doc_id: int, payload: schemas.ApprovalIn, db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    founders = {founder_id(f) for f in doc.case.founders}
    if payload.approver not in founders:
        raise HTTPException(400, "Approver must be one of this case's founders")
    approvals = json.loads(doc.approvals_json)
    if payload.approver not in approvals:
        approvals.append(payload.approver)
    doc.approvals_json = json.dumps(approvals)

    # Approved once every founder has signed off.
    if founders.issubset(approvals):
        doc.status = "approved"
    log_action(db, doc.case_id, payload.approver, "document_approved", f"{doc.doc_type} v{doc.version}")

    # Case is ready to file once the latest version of every document is approved.
    case = doc.case
    latest: dict[str, Document] = {}
    for d in case.documents:
        if d.doc_type not in latest or d.version > latest[d.doc_type].version:
            latest[d.doc_type] = d
    if latest and all(d.status == "approved" for d in latest.values()):
        case.status = "ready_to_file"
    db.commit()
    return doc


@app.get("/cases/{case_id}/audit", response_model=list[schemas.AuditLogOut])
def audit_log(case_id: int, db: Session = Depends(get_db)):
    get_case_or_404(db, case_id)
    return db.query(AuditLog).filter_by(case_id=case_id).order_by(AuditLog.timestamp.desc()).all()
