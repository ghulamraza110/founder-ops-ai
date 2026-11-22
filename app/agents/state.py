"""Shared state and term schemas for the multi-agent incorporation graph."""
import operator
import os
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

AUTHORIZED_SHARES = 10_000_000
DEFAULT_VESTING_MONTHS = 48
DEFAULT_CLIFF_MONTHS = 12
EXPORTS_DIR = Path(os.getenv("EXPORTS_DIR", Path(__file__).resolve().parents[2] / "exports"))


def export_dir(company_name: str) -> Path:
    """exports/<company-slug>/, created on demand."""
    slug = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-") or "company"
    path = EXPORTS_DIR / slug
    path.mkdir(parents=True, exist_ok=True)
    return path


def share_label(entity_type: str) -> str:
    return "units" if entity_type == "LLC" else "shares"


class FounderTerms(BaseModel):
    name: str | None = Field(default=None, description="Founder's full name")
    email: str | None = Field(default=None, description="Founder's email, if given")
    equity_percent: float | None = Field(default=None, description="Ownership percentage, e.g. 60 for 60%")
    vesting_months: int | None = Field(default=None, description="Total vesting period in months")
    cliff_months: int | None = Field(default=None, description="Cliff period in months")


class StartupTerms(BaseModel):
    company_name: str | None = Field(default=None, description="Legal name of the company")
    entity_type: Literal["LLC", "Delaware C-Corp"] | None = Field(
        default=None, description='Either "LLC" or "Delaware C-Corp"'
    )
    incorporation_date: date | None = Field(default=None, description="Incorporation date (YYYY-MM-DD)")
    founders: list[FounderTerms] = Field(default_factory=list)


class GraphState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    terms: dict                 # StartupTerms as JSON-safe dict (locked once terms_ready)
    terms_ready: bool
    questions: list[str]        # follow-ups for the founder when terms are incomplete
    deadline_83b: str           # ISO date
    documents: dict[str, str]   # doc title -> Markdown (legal_drafter)
    cap_table: dict             # JSON summary (cap_table_builder)
    export_files: Annotated[list[str], operator.add]  # both builders append in parallel
    export_name: str            # exports/ subfolder; defaults to the company name


def export_folder(state: "GraphState", terms: StartupTerms) -> Path:
    return export_dir(state.get("export_name") or terms.company_name)
