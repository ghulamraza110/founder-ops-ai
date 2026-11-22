"""Legal Drafter (Builder Agent 1): drafts Markdown legal documents from the locked terms via the LLM."""
import json

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.cap_table_builder import allocate_shares
from app.agents.state import AUTHORIZED_SHARES, GraphState, StartupTerms, export_folder, share_label
from app.llm import get_llm

SYSTEM_PROMPT = """You are a startup paralegal drafting clean, professional Markdown documents.
- Use ONLY the facts provided. Never invent names, numbers, or dates.
- Use bracketed placeholders for anything not provided, e.g. [FOUNDER ADDRESS], [SSN], [SIGNATURE].
- Use headings, numbered sections, and a signature block where appropriate.
- Output only the document Markdown: no preamble, no code fences.
- End with: "_Draft generated for review. Not legal advice; have counsel review before signing or filing._\""""

DOCS = {
    "Founder IP Assignment Agreement": (
        "Draft a Founder Intellectual Property Assignment Agreement between the company and each founder. "
        "Cover assignment of prior and future IP related to the business, moral rights waiver, further "
        "assurances, confidentiality, and governing law ({law}). Include one signature block per founder."
    ),
    "Corporate Bylaws Summary": (
        "Draft a concise summary of the company's {governing_doc}. Cover: management structure, "
        "{holders} meetings and voting, board/managers, officers, the equity and vesting terms of each "
        "founder, transfer restrictions, and amendment procedures. Use a table for founder ownership."
    ),
    "IRS Section 83(b) Election Form": (
        "Draft a pre-filled IRS Section 83(b) election, one complete election per founder. Each must include: "
        "taxpayer name, [ADDRESS], [SSN]; description of property ({shares_word} of the company); "
        "date of transfer (incorporation date); taxable year; nature of restrictions (the founder's vesting "
        "terms); fair market value at transfer [FMV PER SHARE]; amount paid [AMOUNT PAID]; and amount included "
        "in gross income. Put the FILING DEADLINE {deadline} prominently at the top, and add filing "
        "instructions: mail to the IRS by certified mail before the deadline, give a copy to the company, "
        "and keep a copy for records."
    ),
}


def build_facts(terms: StartupTerms, deadline: str) -> dict:
    shares = allocate_shares(terms)
    return {
        "company_name": terms.company_name,
        "entity_type": terms.entity_type,
        "incorporation_date": terms.incorporation_date.isoformat(),
        "authorized_shares": AUTHORIZED_SHARES,
        "unit": share_label(terms.entity_type),
        "irs_83b_deadline": deadline,
        "founders": [
            {**f.model_dump(), "shares": shares[f.name]} for f in terms.founders
        ],
    }


def draft(title: str, instructions: str, facts: dict) -> str:
    prompt = f"Document: {title}\n\n{instructions}\n\nFacts (JSON):\n{json.dumps(facts, indent=2)}"
    resp = get_llm(temperature=0.2).invoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)])
    return resp.content.strip()


def legal_drafter_node(state: GraphState) -> GraphState:
    terms = StartupTerms.model_validate(state["terms"])
    facts = build_facts(terms, state["deadline_83b"])
    is_llc = terms.entity_type == "LLC"
    fmt = {
        "law": "the state of formation" if is_llc else "Delaware",
        "governing_doc": "Operating Agreement (the LLC equivalent of bylaws)" if is_llc else "Bylaws",
        "holders": "member" if is_llc else "stockholder",
        "shares_word": "membership units" if is_llc else "shares of common stock",
        "deadline": state["deadline_83b"],
    }

    out = export_folder(state, terms)
    documents, files = {}, []
    for title, instructions in DOCS.items():
        documents[title] = draft(title, instructions.format(**fmt), facts)
        path = out / (title.lower().replace(" ", "_").replace("(", "").replace(")", "") + ".md")
        path.write_text(documents[title], encoding="utf-8")
        files.append(str(path))
    return {"documents": documents, "export_files": files}
