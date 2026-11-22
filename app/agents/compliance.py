"""Deterministic compliance checks on a case (no LLM needed)."""


def check_compliance(case: dict, founders: list[dict]) -> list[str]:
    issues: list[str] = []
    if not case.get("name"):
        issues.append("Company name is required.")
    if not founders:
        issues.append("At least one founder is required.")
    total = sum(f["equity_percent"] for f in founders)
    if founders and abs(total - 100) > 0.01:
        issues.append(f"Founder equity totals {total:.2f}%, expected 100%.")
    for f in founders:
        if f["cliff_months"] > f["vesting_months"]:
            issues.append(f"{f['name']}: cliff ({f['cliff_months']}m) exceeds vesting ({f['vesting_months']}m).")
        if "@" not in f["email"]:
            issues.append(f"{f['name']}: invalid email '{f['email']}'.")
    return issues
