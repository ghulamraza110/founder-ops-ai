"""Workflow entry points used by the API.

Two ways in, same agents:
- chat: founder message -> full multi-agent graph (manager -> legal_drafter + cap_table_builder)
- form: founders entered in the UI -> deterministic validation -> both builders

Chat state is checkpointed to SQLite per case, so conversations survive API restarts.
"""
import os
import sqlite3

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.agents.cap_table_builder import cap_table_node
from app.agents.graph import build_graph
from app.agents.legal_drafter import legal_drafter_node
from app.agents.manager_agent import calculate_83b_deadline, validate_terms
from app.agents.state import GraphState, StartupTerms

CHECKPOINT_DB = os.getenv("CHECKPOINT_DB", "checkpoints.db")

chat_graph = build_graph(SqliteSaver(sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)))


def _build_builders_graph():
    g = StateGraph(GraphState)
    g.add_node("legal_drafter", legal_drafter_node)
    g.add_node("cap_table_builder", cap_table_node)
    g.add_edge(START, "legal_drafter")
    g.add_edge(START, "cap_table_builder")
    g.add_edge("legal_drafter", END)
    g.add_edge("cap_table_builder", END)
    return g.compile()


builders_graph = _build_builders_graph()


def case_key(case_id: int) -> str:
    """Used as both the chat thread id and the exports/ subfolder name."""
    return f"case-{case_id}"


def _config(case_id: int) -> dict:
    return {"configurable": {"thread_id": case_key(case_id)}}


def run_chat(case_id: int, message: str) -> dict:
    # Clear last turn's outputs so a turn that only asks questions doesn't look like it drafted again.
    return chat_graph.invoke(
        {"messages": [HumanMessage(content=message)], "documents": {}, "cap_table": {},
         "export_name": case_key(case_id)},
        _config(case_id),
    )


def chat_history(case_id: int) -> list[dict]:
    messages = chat_graph.get_state(_config(case_id)).values.get("messages", [])
    return [
        {"role": "user" if isinstance(m, HumanMessage) else "assistant", "content": m.content}
        for m in messages if isinstance(m, (HumanMessage, AIMessage))
    ]


def run_from_terms(case_id: int, terms: StartupTerms) -> dict:
    """Form path: skip the LLM manager, validate in Python, then run both builders in parallel."""
    questions = validate_terms(terms)
    if questions:
        return {"terms_ready": False, "questions": questions}
    deadline = calculate_83b_deadline(terms.incorporation_date).isoformat()
    result = builders_graph.invoke({
        "terms": terms.model_dump(mode="json"),
        "terms_ready": True,
        "deadline_83b": deadline,
        "export_files": [],
        "export_name": case_key(case_id),
    })
    return {**result, "questions": []}
