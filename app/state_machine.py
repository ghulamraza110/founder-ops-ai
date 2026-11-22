"""LangGraph workflow: intake -> compliance check -> draft docs -> awaiting approval."""
from typing import TypedDict

from langgraph.graph import END, StateGraph

from app.agents import check_compliance, draft_document

DOC_TYPES = [
    "Certificate of Incorporation",
    "Bylaws",
    "Founder Stock Purchase Agreement",
]


class CaseState(TypedDict, total=False):
    case: dict
    founders: list[dict]
    issues: list[str]
    drafts: dict[str, str]
    status: str


def compliance_node(state: CaseState) -> CaseState:
    issues = check_compliance(state["case"], state["founders"])
    return {"issues": issues, "status": "blocked" if issues else "compliant"}


def draft_node(state: CaseState) -> CaseState:
    drafts = {t: draft_document(t, state["case"], state["founders"]) for t in DOC_TYPES}
    return {"drafts": drafts, "status": "awaiting_approval"}


def route_after_compliance(state: CaseState) -> str:
    return "draft" if not state["issues"] else END


def build_graph():
    graph = StateGraph(CaseState)
    graph.add_node("compliance", compliance_node)
    graph.add_node("draft", draft_node)
    graph.set_entry_point("compliance")
    graph.add_conditional_edges("compliance", route_after_compliance, {"draft": "draft", END: END})
    graph.add_edge("draft", END)
    return graph.compile()


workflow = build_graph()


def run_workflow(case: dict, founders: list[dict]) -> CaseState:
    return workflow.invoke({"case": case, "founders": founders, "drafts": {}, "issues": []})
