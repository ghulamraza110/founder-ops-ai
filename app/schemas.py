"""Pydantic v2 request/response schemas."""
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class FounderCreate(BaseModel):
    name: str
    email: str = ""
    equity_percent: float = Field(gt=0, le=100)
    vesting_months: int = Field(default=48, ge=1)
    cliff_months: int = Field(default=12, ge=0)


class FounderOut(ORMModel, FounderCreate):
    id: int
    case_id: int


class CaseCreate(BaseModel):
    name: str = "Untitled startup"
    entity_type: Literal["Delaware C-Corp", "LLC"] = "Delaware C-Corp"
    state: str = "Delaware"
    incorporation_date: date | None = None


class CaseOut(ORMModel):
    id: int
    name: str
    entity_type: str
    state: str
    incorporation_date: date | None
    status: str


class CaseDetail(CaseOut):
    founders: list[FounderOut] = []


class DocumentOut(ORMModel):
    id: int
    case_id: int
    doc_type: str
    version: int
    content_md: str
    status: str
    approvals_json: str


class ApprovalIn(BaseModel):
    approver: str = Field(description="Founder email, or name if no email is on file")


class AuditLogOut(ORMModel):
    id: int
    case_id: int
    actor: str
    action: str
    details: str
    timestamp: datetime


class ChatIn(BaseModel):
    message: str = Field(min_length=1)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class WorkflowResult(BaseModel):
    case_id: int
    status: str
    terms_ready: bool
    questions: list[str] = []
    reply: str | None = None          # Manager Agent's message (chat only)
    terms: dict | None = None         # extracted terms so far (chat only)
    deadline_83b: date | None = None
    documents: list[str] = []
    export_files: list[str] = []
