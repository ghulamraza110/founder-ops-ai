"""Multi-party sign-off state machine for documents. Pure Python, no LLM.

    DRAFT ──approve──> PENDING_APPROVAL ──every founder signed the current version──> EXECUTED
      │                   │      ▲                                                        │
      └──edit─────────────┴──> INVALIDATED ──(signatures reset)──┘ <─────────edit──────────┘

- `approvals` maps founder_name -> version they signed, or None.
- A signature only counts for the version it was given on; editing bumps the version, so every
  other founder must sign again. The editor's own signature moves to the new version.
"""
import json
from enum import StrEnum

from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.models import Document, StartupCase, log_action


class DocStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    INVALIDATED = "INVALIDATED"
    EXECUTED = "EXECUTED"


ALLOWED = {
    DocStatus.DRAFT: {DocStatus.PENDING_APPROVAL, DocStatus.EXECUTED, DocStatus.INVALIDATED},
    DocStatus.PENDING_APPROVAL: {DocStatus.EXECUTED, DocStatus.INVALIDATED},
    DocStatus.INVALIDATED: {DocStatus.PENDING_APPROVAL},
    DocStatus.EXECUTED: {DocStatus.INVALIDATED},
}


class SignOffError(ValueError):
    """Raised for an operation the state machine does not allow (mapped to HTTP 400)."""


class VersionConflict(SignOffError):
    """The document changed since the founder loaded it (mapped to HTTP 409)."""


# ---------- helpers ----------

def founder_names(case: StartupCase) -> list[str]:
    return [f.name for f in case.founders]


def get_approvals(doc: Document) -> dict[str, int | None]:
    """Approvals for the case's current founders. Founders with no entry yet count as unsigned."""
    raw = json.loads(doc.approvals_json or "{}")
    if isinstance(raw, list):  # rows written before approvals became a dict
        raw = {}
    return {name: raw.get(name) for name in founder_names(doc.case)}


def _set_approvals(doc: Document, approvals: dict[str, int | None]) -> None:
    doc.approvals_json = json.dumps(approvals)


def status_of(doc: Document) -> DocStatus:
    try:
        return DocStatus(doc.status.upper())
    except ValueError:  # legacy lowercase/unknown statuses
        return DocStatus.DRAFT


def _transition(db: Session, doc: Document, to: DocStatus, actor: str) -> None:
    current = status_of(doc)
    if current == to:
        return
    if to not in ALLOWED[current]:
        raise SignOffError(f"Cannot move {doc.doc_type} from {current} to {to}")
    doc.status = to.value
    log_action(db, doc.case_id, actor, "status_changed", f"{doc.doc_type} v{doc.version}: {current} -> {to}")


def _all_signed(doc: Document, approvals: dict[str, int | None]) -> bool:
    return bool(approvals) and all(v == doc.version for v in approvals.values())


def _require_founder(doc: Document, founder_name: str) -> None:
    if founder_name not in founder_names(doc.case):
        raise SignOffError(f"'{founder_name}' is not a founder of this case")


def sync_case_status(case: StartupCase) -> None:
    """Case is ready to file once every document is EXECUTED."""
    if not case.documents:
        return
    if all(status_of(d) == DocStatus.EXECUTED for d in case.documents):
        case.status = "ready_to_file"
    elif case.status == "ready_to_file":
        case.status = "awaiting_approval"


def _new_version(db: Session, doc: Document, content_md: str, editor: str | None, actor: str) -> list[str]:
    """Bump version, reset signatures (keeping the editor's), and pass through INVALIDATED.

    Returns the founders whose existing signatures were invalidated.
    """
    before = get_approvals(doc)
    invalidated = [n for n, v in before.items() if v is not None and n != editor]
    old_version = doc.version

    _transition(db, doc, DocStatus.INVALIDATED, actor)  # logged against the old version
    doc.version += 1
    doc.content_md = content_md
    approvals = {n: (doc.version if n == editor else None) for n in before}
    _set_approvals(doc, approvals)

    log_action(db, doc.case_id, actor, "document_edited", json.dumps({
        "document": doc.doc_type,
        "version": f"v{old_version} -> v{doc.version}",
        "edited_by": editor or actor,
        "invalidated_signatures": invalidated,
    }))
    _transition(db, doc, DocStatus.PENDING_APPROVAL, actor)
    if _all_signed(doc, approvals):  # e.g. a sole founder editing their own document
        _transition(db, doc, DocStatus.EXECUTED, actor)
    sync_case_status(doc.case)
    return invalidated


# ---------- public operations ----------

def create_document(db: Session, case: StartupCase, doc_type: str, content_md: str, actor: str) -> Document:
    doc = Document(case_id=case.id, doc_type=doc_type, version=1, content_md=content_md,
                   status=DocStatus.DRAFT.value,
                   approvals_json=json.dumps({n: None for n in founder_names(case)}))
    case.documents.append(doc)
    log_action(db, case.id, actor, "document_drafted", f"{doc_type} v1")
    return doc


def redraft_document(db: Session, doc: Document, content_md: str, actor: str) -> Document:
    """Agents regenerated the document: new version, every signature reset."""
    _new_version(db, doc, content_md, editor=None, actor=actor)
    return doc


def _check_version(doc: Document, expected: int | None, verb: str) -> None:
    if expected is not None and expected != doc.version:
        raise VersionConflict(
            f"Cannot {verb} v{expected} of {doc.doc_type}: it is now v{doc.version}. "
            "Reload and review the latest version first."
        )


def _commit(db: Session, doc: Document) -> None:
    """Commit, turning a lost optimistic-lock race into a VersionConflict."""
    try:
        db.commit()
    except StaleDataError:
        db.rollback()
        raise VersionConflict(f"{doc.doc_type} was changed by someone else just now. Reload and try again.")


def approve(db: Session, doc_id: int, founder_name: str, expected_version: int | None = None) -> Document:
    """Sign the current version. Pass `expected_version` (the version the founder read) so a
    signature can never land on a version they have not seen."""
    doc = db.get(Document, doc_id)
    if not doc:
        raise LookupError("Document not found")
    _require_founder(doc, founder_name)
    _check_version(doc, expected_version, "approve")
    if status_of(doc) == DocStatus.EXECUTED:
        return doc  # already fully signed; nothing to do

    approvals = get_approvals(doc)
    approvals[founder_name] = doc.version
    _set_approvals(doc, approvals)
    log_action(db, doc.case_id, founder_name, "document_approved", f"{doc.doc_type} v{doc.version}")

    if _all_signed(doc, approvals):
        _transition(db, doc, DocStatus.EXECUTED, founder_name)
    else:
        _transition(db, doc, DocStatus.PENDING_APPROVAL, founder_name)
    sync_case_status(doc.case)
    _commit(db, doc)
    return doc


def edit_document(db: Session, doc_id: int, founder_name: str, new_content_md: str,
                  base_version: int | None = None) -> tuple[Document, list[str]]:
    """Save a new version. Pass `base_version` (the version the edit was based on) so two
    founders editing at once cannot silently overwrite each other."""
    doc = db.get(Document, doc_id)
    if not doc:
        raise LookupError("Document not found")
    _require_founder(doc, founder_name)
    _check_version(doc, base_version, "edit")
    if not new_content_md.strip():
        raise SignOffError("Document content cannot be empty")
    if new_content_md == doc.content_md:
        raise SignOffError("No changes to save")

    invalidated = _new_version(db, doc, new_content_md, editor=founder_name, actor=founder_name)
    _commit(db, doc)
    return doc, invalidated
