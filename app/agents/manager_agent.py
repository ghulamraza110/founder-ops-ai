"""Manager Agent: turns founder chat into locked, validated startup terms.

The LLM only *extracts* terms. Every rule (equity == 100%, required fields,
83(b) deadline) is checked in plain Python so results are deterministic.
"""
from datetime import date, timedelta

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.state import (
    DEFAULT_CLIFF_MONTHS,
    DEFAULT_VESTING_MONTHS,
    GraphState,
    StartupTerms,
)
from app.llm import get_llm

EXTRACTION_PROMPT = """You extract startup incorporation terms from a founder conversation.
Today's date is {today}.

Current known terms (JSON):
{current}

Return the COMPLETE updated terms: keep known values unless the founder changed them,
and add anything new from the conversation. Rules:
- entity_type must be exactly "LLC" or "Delaware C-Corp" ("C-Corp", "corporation", "Delaware corp" -> "Delaware C-Corp").
- Dates as YYYY-MM-DD; resolve relative dates ("today", "next Monday") against today's date.
- equity_percent is a number (60 means 60%).
- "standard vesting" means vesting_months=48 and cliff_months=12.
- Never invent values. Leave a field null if the founder has not stated it."""

EQUITY_TOLERANCE = 0.01


def calculate_83b_deadline(incorporation_date: date) -> date:
    """IRS 83(b) election must be filed within 30 days of the stock grant.

    Assumes founder stock is granted on the incorporation date.
    """
    return incorporation_date + timedelta(days=30)


def extract_terms(messages: list, current: StartupTerms) -> StartupTerms:
    extractor = get_llm(temperature=0).with_structured_output(StartupTerms, method="json_schema")
    system = SystemMessage(content=EXTRACTION_PROMPT.format(
        today=date.today().isoformat(),
        current=current.model_dump_json(indent=2),
    ))
    convo = [m for m in messages if isinstance(m, (HumanMessage, AIMessage))]
    return extractor.invoke([system, *convo])


def apply_defaults(terms: StartupTerms) -> StartupTerms:
    for f in terms.founders:
        if f.vesting_months is None:
            f.vesting_months = DEFAULT_VESTING_MONTHS
        if f.cliff_months is None:
            f.cliff_months = DEFAULT_CLIFF_MONTHS
    return terms


def validate_terms(terms: StartupTerms) -> list[str]:
    """Return concise follow-up questions; empty list means terms are complete and valid."""
    q: list[str] = []
    if not terms.company_name:
        q.append("What is the company's legal name?")
    if not terms.entity_type:
        q.append("Should this be an LLC or a Delaware C-Corp?")
    if not terms.incorporation_date:
        q.append("What is the incorporation date (YYYY-MM-DD)?")
    if not terms.founders:
        q.append("Who are the founders, and what equity % does each hold?")
        return q

    names = [f.name for f in terms.founders]
    if any(not n for n in names):
        q.append("What are the full names of all founders?")
    elif len(set(n.lower() for n in names)) != len(names):
        q.append("Two founders share the same name. Can you give their full names?")

    missing_equity = [f.name or "a founder" for f in terms.founders if f.equity_percent is None]
    if missing_equity:
        q.append(f"What equity % does {', '.join(missing_equity)} hold?")
    else:
        if any(f.equity_percent <= 0 for f in terms.founders):
            q.append("Every founder's equity must be above 0%. Can you correct the split?")
        total = sum(f.equity_percent for f in terms.founders)
        if abs(total - 100) > EQUITY_TOLERANCE:
            split = ", ".join(f"{f.name} {f.equity_percent:g}%" for f in terms.founders)
            q.append(f"Equity adds up to {total:g}% ({split}), not 100%. What is the correct split?")

    for f in terms.founders:
        if f.vesting_months is not None and f.vesting_months <= 0:
            q.append(f"{f.name}'s vesting period must be at least 1 month. How long should it be?")
        elif f.cliff_months is not None and f.vesting_months is not None and f.cliff_months > f.vesting_months:
            q.append(f"{f.name}'s cliff ({f.cliff_months}m) is longer than vesting ({f.vesting_months}m). Which is right?")
    return q


def summarize(terms: StartupTerms, deadline: date) -> str:
    lines = [
        f"Terms locked for **{terms.company_name}** ({terms.entity_type}), incorporated {terms.incorporation_date}.",
        f"IRS 83(b) election deadline: **{deadline.isoformat()}** (incorporation date + 30 days).",
    ]
    if deadline.weekday() >= 5:
        lines.append("Note: that date falls on a weekend; file before then to be safe.")
    for f in terms.founders:
        lines.append(f"- {f.name}: {f.equity_percent:g}%, {f.vesting_months}-month vesting, {f.cliff_months}-month cliff")
    lines.append("Drafting legal documents and building the cap table now.")
    return "\n".join(lines)


def manager_node(state: GraphState) -> GraphState:
    current = StartupTerms.model_validate(state.get("terms") or {})
    terms = apply_defaults(extract_terms(state["messages"], current))
    questions = validate_terms(terms)

    if questions:
        reply = "I need a bit more information:\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(questions, 1))
        return {
            "terms": terms.model_dump(mode="json"),
            "terms_ready": False,
            "questions": questions,
            "messages": [AIMessage(content=reply, name="manager")],
        }

    deadline = calculate_83b_deadline(terms.incorporation_date)
    return {
        "terms": terms.model_dump(mode="json"),
        "terms_ready": True,
        "questions": [],
        "deadline_83b": deadline.isoformat(),
        "messages": [AIMessage(content=summarize(terms, deadline), name="manager")],
    }


def route_after_manager(state: GraphState) -> list[str] | str:
    """Fan out to both builders in parallel once terms are ready; otherwise wait for the founder."""
    if state.get("terms_ready"):
        return ["legal_drafter", "cap_table_builder"]
    return "__end__"
