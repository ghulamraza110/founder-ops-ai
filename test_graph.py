"""CLI test for the multi-agent incorporation graph.

    python test_graph.py --unit    # deterministic checks only, no API calls
    python test_graph.py --ping    # one tiny LLM call to check the API key/model
    python test_graph.py --demo    # scripted 2-turn conversation through the full graph
    python test_graph.py           # interactive chat with the Manager Agent
"""
import argparse
import os
import sys
import time
import uuid
from datetime import date

from langchain_core.messages import HumanMessage

DEMO_TURNS = [
    # Turn 1: equity adds to 90% and date missing -> Manager should ask follow-ups.
    "We're starting Nova Robotics as a Delaware C-Corp. Founders: Alice Khan (alice@nova.ai) 60% "
    "and Bob Lee (bob@nova.ai) 30%. Standard 4-year vesting with a 1-year cliff.",
    # Turn 2: fixes both -> terms_ready, both builders run.
    "Sorry, Bob has 40%. We incorporate on 2026-12-01.",
]


def provider_info() -> str:
    from app.llm import PROVIDERS
    p = os.getenv("LLM_PROVIDER", "deepseek").lower()
    return f"{p} / {os.getenv('LLM_MODEL') or PROVIDERS.get(p, {}).get('model', '?')}"


def run_unit() -> int:
    from app.agents.cap_table_builder import allocate_shares, build_cap_table
    from app.agents.manager_agent import apply_defaults, calculate_83b_deadline, validate_terms
    from app.agents.state import StartupTerms

    def terms(**kw):
        base = {"company_name": "Acme", "entity_type": "Delaware C-Corp", "incorporation_date": "2026-12-01",
                "founders": [{"name": "A", "equity_percent": 60}, {"name": "B", "equity_percent": 40}]}
        return apply_defaults(StartupTerms.model_validate({**base, **kw}))

    thirds = terms(founders=[{"name": n, "equity_percent": 100 / 3} for n in "XYZ"])
    cap = build_cap_table(terms())
    a = cap["founders"][0]
    checks = {
        "valid terms -> no questions": validate_terms(terms()) == [],
        "equity 90% -> question": any("90%" in q for q in validate_terms(
            terms(founders=[{"name": "A", "equity_percent": 60}, {"name": "B", "equity_percent": 30}]))),
        "missing date/entity/name -> 3 questions": len(validate_terms(
            terms(company_name=None, entity_type=None, incorporation_date=None))) == 3,
        "cliff > vesting -> question": bool(validate_terms(
            terms(founders=[{"name": "A", "equity_percent": 100, "vesting_months": 12, "cliff_months": 24}]))),
        "defaults 48m vesting / 12m cliff": a["vesting_months"] == 48 and a["cliff_months"] == 12,
        "83(b) = incorporation + 30 days": calculate_83b_deadline(date(2026, 12, 1)) == date(2026, 12, 31),
        "83(b) crosses month/year": calculate_83b_deadline(date(2026, 12, 15)) == date(2027, 1, 14),
        "60/40 -> 6,000,000 / 4,000,000": [f["shares"] for f in cap["founders"]] == [6_000_000, 4_000_000],
        "1/3 splits sum to exactly 10M": sum(allocate_shares(thirds).values()) == 10_000_000,
        "nothing vested before cliff": a["shares_vested_at_cliff"] == 1_500_000 and a["cliff_date"] == "2027-12-01",
        "fully vested after 48 months": a["fully_vested_date"] == "2030-12-01",
    }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    passed = sum(checks.values())
    print(f"\n{passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


def run_ping() -> int:
    from app.llm import get_llm
    print(f"Provider: {provider_info()}")
    t = time.time()
    reply = get_llm().invoke("Reply with exactly: OK").content
    print(f"Reply: {reply!r} ({time.time() - t:.1f}s)")
    return 0


def show(result: dict) -> None:
    print(f"\nManager > {result['messages'][-1].content if not result.get('terms_ready') else ''}", end="")
    if not result.get("terms_ready"):
        print()
        return
    manager_msg = next(m for m in reversed(result["messages"]) if getattr(m, "name", None) == "manager")
    print(manager_msg.content)

    cap = result.get("cap_table", {})
    print(f"\n--- Cap table ({cap.get('authorized_shares', 0):,} authorized {cap.get('unit')}) ---")
    for f in cap.get("founders", []):
        print(f"  {f['name']:<20} {f['equity_percent']:>7.2f}%  {f['shares']:>12,}  "
              f"cliff {f['cliff_date']} ({f['shares_vested_at_cliff']:,})  fully vested {f['fully_vested_date']}")

    print("\n--- Documents ---")
    for title, md in result.get("documents", {}).items():
        print(f"  {title}: {len(md):,} chars")

    print("\n--- Exported files ---")
    for path in dict.fromkeys(result.get("export_files", [])):  # de-dupe across turns, keep order
        print(f"  {path}")


def chat(graph, config, text: str) -> dict:
    t = time.time()
    result = graph.invoke({"messages": [HumanMessage(content=text)]}, config)
    show(result)
    print(f"\n({time.time() - t:.1f}s)")
    return result


def run_demo() -> int:
    from app.agents.graph import incorporation_graph
    print(f"Provider: {provider_info()}")
    config = {"configurable": {"thread_id": f"demo-{uuid.uuid4().hex[:8]}"}}
    result = {}
    for turn in DEMO_TURNS:
        print(f"\n{'=' * 70}\nFounder > {turn}")
        result = chat(incorporation_graph, config, turn)
    ok = result.get("terms_ready") and len(result.get("documents", {})) == 3 and result.get("cap_table")
    print(f"\n{'DEMO PASSED' if ok else 'DEMO FAILED'}")
    return 0 if ok else 1


def run_interactive() -> int:
    from app.agents.graph import incorporation_graph
    print(f"Provider: {provider_info()}\nDescribe your startup (type 'quit' to exit).")
    config = {"configurable": {"thread_id": f"cli-{uuid.uuid4().hex[:8]}"}}
    while True:
        try:
            text = input("\nFounder > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"quit", "exit", "q"}:
            break
        if text:
            chat(incorporation_graph, config, text)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--unit", action="store_true", help="deterministic checks, no API calls")
    mode.add_argument("--ping", action="store_true", help="one tiny LLM call")
    mode.add_argument("--demo", action="store_true", help="scripted conversation through the full graph")
    args = parser.parse_args()
    sys.exit(run_unit() if args.unit else run_ping() if args.ping else run_demo() if args.demo else run_interactive())
