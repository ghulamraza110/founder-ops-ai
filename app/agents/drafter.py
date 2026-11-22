"""LLM agent that drafts incorporation documents in Markdown."""
import json

from langchain_core.messages import HumanMessage, SystemMessage

from app.llm import get_llm

SYSTEM_PROMPT = (
    "You are a startup incorporation paralegal assistant. Draft clear, well-structured "
    "Markdown documents. Use placeholders like [SIGNATURE] where needed. Add a short "
    "disclaimer at the end that the draft is not legal advice and must be reviewed by counsel."
)


def draft_document(doc_type: str, case: dict, founders: list[dict]) -> str:
    prompt = (
        f"Draft a {doc_type} for this company.\n\n"
        f"Company:\n{json.dumps(case, default=str, indent=2)}\n\n"
        f"Founders:\n{json.dumps(founders, default=str, indent=2)}"
    )
    response = get_llm().invoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)])
    return response.content
