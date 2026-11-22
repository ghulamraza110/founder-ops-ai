"""Cap Table Builder (Builder Agent 2): deterministic share math, vesting, CSV/Excel export. No LLM."""
import calendar
import csv
import json
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from app.agents.state import AUTHORIZED_SHARES, GraphState, StartupTerms, export_folder, share_label


def allocate_shares(terms: StartupTerms, total: int = AUTHORIZED_SHARES) -> dict[str, int]:
    """Split `total` shares by equity %, using largest-remainder rounding so the sum is exact."""
    raw = {f.name: total * f.equity_percent / 100 for f in terms.founders}
    shares = {n: int(v) for n, v in raw.items()}
    leftover = total - sum(shares.values())
    for name in sorted(raw, key=lambda n: raw[n] - shares[n], reverse=True)[:leftover]:
        shares[name] += 1
    return shares


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def vesting_schedule(shares: int, start: date, vesting_months: int, cliff_months: int) -> list[dict]:
    """Monthly schedule. Nothing vests before the cliff; at the cliff the accrued portion vests at once,
    then the rest vests monthly. Integer shares; the final month always reaches 100%."""
    rows, prev = [], 0
    for m in range(1, vesting_months + 1):
        cumulative = 0 if m < cliff_months else shares * m // vesting_months
        rows.append({
            "month": m,
            "date": add_months(start, m).isoformat(),
            "vested_this_month": cumulative - prev,
            "cumulative_vested": cumulative,
            "unvested": shares - cumulative,
            "percent_vested": round(cumulative / shares * 100, 4) if shares else 0,
        })
        prev = cumulative
    return rows


def build_cap_table(terms: StartupTerms) -> dict:
    allocation = allocate_shares(terms)
    founders = []
    for f in terms.founders:
        schedule = vesting_schedule(allocation[f.name], terms.incorporation_date, f.vesting_months, f.cliff_months)
        cliff_row = schedule[f.cliff_months - 1] if f.cliff_months else None
        founders.append({
            "name": f.name,
            "email": f.email,
            "equity_percent": f.equity_percent,
            "shares": allocation[f.name],
            "vesting_months": f.vesting_months,
            "cliff_months": f.cliff_months,
            "vesting_start": terms.incorporation_date.isoformat(),
            "cliff_date": cliff_row["date"] if cliff_row else None,
            "shares_vested_at_cliff": cliff_row["cumulative_vested"] if cliff_row else 0,
            "fully_vested_date": schedule[-1]["date"],
            "schedule": schedule,
        })
    return {
        "company_name": terms.company_name,
        "entity_type": terms.entity_type,
        "unit": share_label(terms.entity_type),
        "authorized_shares": AUTHORIZED_SHARES,
        "issued_shares": sum(allocation.values()),
        "founders": founders,
    }


def export_cap_table(cap: dict, out: Path) -> list[str]:
    summary_cols = ["name", "email", "equity_percent", "shares", "vesting_months", "cliff_months",
                    "vesting_start", "cliff_date", "shares_vested_at_cliff", "fully_vested_date"]
    sched_cols = ["founder", "month", "date", "vested_this_month", "cumulative_vested", "unvested", "percent_vested"]
    sched_rows = [{"founder": f["name"], **r} for f in cap["founders"] for r in f["schedule"]]

    paths = {
        "json": out / "cap_table.json",
        "summary_csv": out / "cap_table.csv",
        "schedule_csv": out / "vesting_schedule.csv",
        "xlsx": out / "cap_table.xlsx",
    }
    paths["json"].write_text(json.dumps(cap, indent=2), encoding="utf-8")

    with paths["summary_csv"].open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=summary_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(cap["founders"])
    with paths["schedule_csv"].open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=sched_cols)
        w.writeheader()
        w.writerows(sched_rows)

    wb = Workbook()
    for title, cols, rows in [("Cap Table", summary_cols, cap["founders"]),
                              ("Vesting Schedule", sched_cols, sched_rows)]:
        ws = wb.active if title == "Cap Table" else wb.create_sheet(title)
        ws.title = title
        ws.append(cols)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for r in rows:
            ws.append([r.get(c) for c in cols])
    ws = wb["Cap Table"]
    ws.append([])
    ws.append(["Authorized shares", cap["authorized_shares"]])
    ws.append(["Issued shares", cap["issued_shares"]])
    wb.save(paths["xlsx"])
    return [str(p) for p in paths.values()]


def cap_table_node(state: GraphState) -> GraphState:
    terms = StartupTerms.model_validate(state["terms"])
    cap = build_cap_table(terms)
    files = export_cap_table(cap, export_folder(state, terms))
    # Keep the state summary light: drop per-month rows (they live in the exported files).
    summary = {**cap, "founders": [{k: v for k, v in f.items() if k != "schedule"} for f in cap["founders"]]}
    return {"cap_table": summary, "export_files": files}
