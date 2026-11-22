"""Multi-agent incorporation graph.

    founder chat ─> manager ──(terms incomplete)──> END  (follow-up questions)
                        └──(terms_ready)──┬─> legal_drafter ─────┐
                                          └─> cap_table_builder ─┴─> END

A checkpointer keeps state per thread_id, so each new founder message continues the same case.
"""
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.cap_table_builder import cap_table_node
from app.agents.legal_drafter import legal_drafter_node
from app.agents.manager_agent import manager_node, route_after_manager
from app.agents.state import GraphState


def build_graph(checkpointer=None):
    g = StateGraph(GraphState)
    g.add_node("manager", manager_node)
    g.add_node("legal_drafter", legal_drafter_node)
    g.add_node("cap_table_builder", cap_table_node)

    g.add_edge(START, "manager")
    g.add_conditional_edges("manager", route_after_manager, ["legal_drafter", "cap_table_builder", END])
    g.add_edge("legal_drafter", END)
    g.add_edge("cap_table_builder", END)
    return g.compile(checkpointer=checkpointer or MemorySaver())


incorporation_graph = build_graph()
