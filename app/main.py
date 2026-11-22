"""FastAPI entrypoint. Run: uvicorn app.main:app --reload"""
import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from app import schemas
from app.models import AuditLog, Document, Founder, StartupCase, get_db, init_db, log_action
from app.state_machine import run_workflow


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Startup Incorporation & Compliance Engine", lifespan=lifespan)


def get_case_or_404(db: Session, case_id: int) -> StartupCase:
    case = db.get(StartupCase, case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    return case


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


@app.post("/cases/{case_id}/run", response_model=schemas.RunResult)
def run_case(case_id: int, db: Session = Depends(get_db)):
    case = get_case_or_404(db, case_id)
    case_dict = schemas.CaseOut.model_validate(case).model_dump()
    founders = [schemas.FounderOut.model_validate(f).model_dump() for f in case.founders]

    try:
        result = run_workflow(case_dict, founders)
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    for doc_type, content in result.get("drafts", {}).items():
        latest = (
            db.query(Document)
            .filter_by(case_id=case_id, doc_type=doc_type)
            .order_by(Document.version.desc())
            .first()
        )
        db.add(Document(
            case_id=case_id,
            doc_type=doc_type,
            version=(latest.version + 1) if latest else 1,
            content_md=content,
        ))
        log_action(db, case_id, "drafter_agent", "document_drafted", doc_type)

    case.status = result["status"]
    log_action(db, case_id, "workflow", "workflow_run", json.dumps({"status": case.status, "issues": result["issues"]}))
    db.commit()
    return schemas.RunResult(
        case_id=case_id,
        status=case.status,
        issues=result["issues"],
        documents=list(result.get("drafts", {}).keys()),
    )


@app.get("/cases/{case_id}/documents", response_model=list[schemas.DocumentOut])
def list_documents(case_id: int, db: Session = Depends(get_db)):
    get_case_or_404(db, case_id)
    return db.query(Document).filter_by(case_id=case_id).order_by(Document.doc_type, Document.version.desc()).all()


@app.post("/documents/{doc_id}/approve", response_model=schemas.DocumentOut)
def approve_document(doc_id: int, payload: schemas.ApprovalIn, db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    approvals = json.loads(doc.approvals_json)
    if payload.approver not in approvals:
        approvals.append(payload.approver)
    doc.approvals_json = json.dumps(approvals)

    # Approved once every founder has signed off.
    founder_emails = {f.email for f in doc.case.founders}
    if founder_emails and founder_emails.issubset(approvals):
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
