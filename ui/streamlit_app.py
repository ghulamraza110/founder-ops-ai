"""Demo UI for the Startup Incorporation & Compliance Engine.

Run (with the API running):  streamlit run ui/streamlit_app.py
"""
import json
import os
import string
from datetime import date

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
API = os.getenv("API_URL", "http://localhost:8000")

st.set_page_config(page_title="Founder Ops AI", layout="wide")

STATUS_BADGE = {
    "DRAFT": ":gray-background[DRAFT]",
    "PENDING_APPROVAL": ":orange-background[PENDING APPROVAL]",
    "INVALIDATED": ":red-background[INVALIDATED]",
    "EXECUTED": ":green-background[EXECUTED]",
}
EXAMPLE_PROMPT = ("We're starting Nova Robotics as a Delaware C-Corp. Alice Khan (alice@nova.ai) gets 60% "
                  "and Bob Lee 30%. Standard 4-year vesting with a 1-year cliff.")


# ---------- API helpers ----------

def call(method: str, path: str, **kwargs) -> requests.Response:
    try:
        return requests.request(method, f"{API}{path}", timeout=300, **kwargs)
    except requests.ConnectionError:
        st.error(f"Cannot reach the API at {API}. Start it with `uvicorn app.main:app --reload`.")
        st.stop()


def api(method: str, path: str, **kwargs):
    r = call(method, path, **kwargs)
    if r.ok:
        return r.json()
    try:
        detail = r.json().get("detail", r.text)
    except ValueError:
        detail = r.text
    if r.status_code == 409:
        st.warning(f"**Someone changed this document first.** {detail}")
    else:
        st.error(f"{r.status_code}: {detail}")
    return None


def flash(msg: str, kind: str = "success") -> None:
    """Show a message after the next rerun."""
    st.session_state["flash"] = (kind, msg)


def badge(status: str) -> str:
    return STATUS_BADGE.get(status, status)


# ---------- domain helpers ----------

def edit_events(audit: list[dict]) -> list[dict]:
    """document_edited audit rows, parsed."""
    out = []
    for a in audit:
        if a["action"] == "document_edited":
            try:
                out.append({**json.loads(a["details"]), "timestamp": a["timestamp"]})
            except ValueError:
                pass
    return out


def last_invalidation(doc: dict, audit: list[dict]) -> dict | None:
    """The edit that produced the document's current version, if any."""
    for e in edit_events(audit):  # audit is newest first
        if e.get("document") == doc["doc_type"] and e.get("version", "").endswith(f"v{doc['version']}"):
            return e
    return None


def split_clauses(md: str) -> list[tuple[str, str]]:
    """Split Markdown into (heading, text) sections. Joining the texts gives back the original."""
    sections: list[tuple[str, str]] = []
    title, buf = "(Preamble)", []
    for line in md.splitlines(keepends=True):
        if line.lstrip().startswith("#"):
            if buf:
                sections.append((title, "".join(buf)))
            title, buf = line.strip().lstrip("#").strip() or "(Untitled)", [line]
        else:
            buf.append(line)
    if buf:
        sections.append((title, "".join(buf)))
    return sections


def vested_on(founder: dict, day: date) -> int:
    vested = 0
    for row in founder.get("schedule", []):
        if date.fromisoformat(row["date"]) <= day:
            vested = row["cumulative_vested"]
    return vested


# ---------- sidebar ----------

health = api("GET", "/health") or {}
llm_label = f"{health.get('llm_provider', '?')} · {health.get('llm_model', '?')}"

with st.sidebar:
    st.header("Founder Ops AI")
    if st.button("New case", type="primary", width="stretch"):
        st.session_state["case_id"] = None
        st.rerun()

    cases = api("GET", "/cases") or []
    ids = [c["id"] for c in cases]
    labels = {c["id"]: f"#{c['id']} {c['name']} ({c['status']})" for c in cases}
    options = [None, *ids]
    current = st.session_state.get("case_id", ids[0] if ids else None)
    case_id = st.selectbox("Case", options, index=options.index(current) if current in options else 0,
                           format_func=lambda i: "New case (start chatting)" if i is None else labels[i])
    st.session_state["case_id"] = case_id

case = api("GET", f"/cases/{case_id}") if case_id else None
founders = [f["name"] for f in case["founders"]] if case else []

with st.sidebar:
    st.divider()
    st.subheader("Active User")
    if founders:
        role_labels = {n: f"Founder {string.ascii_uppercase[i]} · {n}" for i, n in enumerate(founders)}
        key = f"active_user_{case_id}"
        if st.session_state.get(key) not in founders:
            st.session_state[key] = founders[0]
        active_user = st.radio("Acting as", founders, key=key, format_func=role_labels.get,
                               label_visibility="collapsed")
        st.caption("Approvals and edits are made as this founder.")
    else:
        active_user = None
        st.caption("Founders appear here once the Manager Agent locks the terms.")

    st.divider()
    st.subheader("Compliance Deadlines")
    if case and case["deadline_83b"]:
        deadline = date.fromisoformat(case["deadline_83b"])
        inc = date.fromisoformat(case["incorporation_date"]) if case["incorporation_date"] else None
        today = date.today()
        days_left = (deadline - today).days
        if days_left < 0:
            st.error(f"**IRS 83(b) election overdue** by {-days_left} days (was due {deadline:%b %d, %Y}).")
        else:
            st.metric("IRS 83(b) election", f"{days_left} days left", help="Incorporation date + 30 days. "
                      "Missing it can mean tax on the full value of vested shares later.")
            st.caption(f"Due **{deadline:%a, %b %d, %Y}**")
            if inc and today < inc:
                st.caption(f"The 30-day window opens on incorporation ({inc:%b %d, %Y}).")
            elif inc:
                st.progress(min(max((today - inc).days / 30, 0.0), 1.0),
                            text=f"Day {min((today - inc).days, 30)} of 30")
            if days_left <= 7:
                st.warning("Less than a week left: mail the election by certified mail now.")
        form = next((d for d in case["documents"] if "83(b)" in d["doc_type"]), None)
        if form:
            st.markdown(f"83(b) form v{form['version']}: {badge(form['status'])}")
        if case["cap_table"]:
            cliffs = sorted({f["cliff_date"] for f in case["cap_table"]["founders"] if f["cliff_date"]})
            if cliffs:
                st.caption(f"Next vesting cliff: **{cliffs[0]}**")
    else:
        st.caption("The 83(b) countdown starts once an incorporation date is agreed.")

if msg := st.session_state.pop("flash", None):
    getattr(st, msg[0])(msg[1])


# ---------- new case: chat only ----------

st.title("Startup Incorporation & Compliance Engine")

if case is None:
    st.subheader("Start a new case")
    st.caption(f"Tell the Manager Agent ({llm_label}) about your startup: company name, LLC or Delaware "
               "C-Corp, founders and their equity, vesting, and incorporation date.")
    st.code(EXAMPLE_PROMPT, language=None, wrap_lines=True)
    if prompt := st.chat_input("Message the Manager Agent"):
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.spinner("Manager Agent is reading your terms..."):
            result = api("POST", "/cases/chat", json={"message": prompt})
        if result:
            st.session_state["case_id"] = result["case_id"]
            st.rerun()
    st.stop()

meta = f"{case['entity_type']} · {case['state']} · status **{case['status']}**"
if case["incorporation_date"]:
    meta += f" · incorporated {case['incorporation_date']}"
st.subheader(case["name"])
st.caption(meta)

tab_chat, tab_cap, tab_docs, tab_audit = st.tabs(
    ["Manager Chat", "Cap Table", "Document Review & Sign-Off", "Audit Trail"])


# ---------- Tab 1: Manager Chat ----------

with tab_chat:
    st.caption(f"Negotiate equity, vesting and entity type with the Manager Agent ({llm_label}). "
               "Once the terms are complete and add up to 100%, it locks them and the builder agents "
               "draft the documents and the cap table.")
    if case["terms"]:
        with st.expander("Current terms", expanded=False):
            t = case["terms"]
            st.markdown(f"**{t.get('company_name') or '—'}** · {t.get('entity_type') or 'entity type ?'} · "
                        f"incorporation {t.get('incorporation_date') or '?'}")
            if t.get("founders"):
                st.dataframe([{"Founder": f["name"], "Equity %": f["equity_percent"],
                               "Vesting (months)": f["vesting_months"], "Cliff (months)": f["cliff_months"]}
                              for f in t["founders"]], hide_index=True, width="stretch")
    for m in api("GET", f"/cases/{case_id}/chat") or []:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
    if prompt := st.chat_input("Message the Manager Agent"):
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.spinner("Agents working (drafting takes ~15s)..."):
            result = api("POST", "/cases/chat", json={"case_id": case_id, "message": prompt})
        if result:
            if result["terms_ready"] and result["documents"]:
                flash("Terms locked. Documents drafted and cap table built: see the other tabs.")
            st.rerun()


# ---------- Tab 2: Cap Table ----------

with tab_cap:
    cap = case["cap_table"]
    if not cap:
        st.info("No cap table yet. Finish the chat with the Manager Agent first.")
    else:
        unit = cap["unit"]
        today = date.today()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric(f"Authorized {unit}", f"{cap['authorized_shares']:,}")
        c2.metric(f"Issued {unit}", f"{cap['issued_shares']:,}")
        c3.metric("Founders", len(cap["founders"]))
        c4.metric(f"Vested as of today", f"{sum(vested_on(f, today) for f in cap['founders']):,}")

        st.markdown("**Ownership**")
        st.dataframe(
            [{"Founder": f["name"],
              "Ownership %": f["equity_percent"],
              unit.title(): f["shares"],
              "Vested today": vested_on(f, today),
              "Unvested": f["shares"] - vested_on(f, today),
              "Vesting": f"{f['vesting_months']} mo / {f['cliff_months']} mo cliff",
              "Cliff date": f["cliff_date"],
              f"Vests at cliff": f["shares_vested_at_cliff"],
              "Fully vested": f["fully_vested_date"]} for f in cap["founders"]],
            hide_index=True, width="stretch",
            column_config={"Ownership %": st.column_config.NumberColumn(format="%.2f%%"),
                           unit.title(): st.column_config.NumberColumn(format="localized"),
                           "Vested today": st.column_config.NumberColumn(format="localized"),
                           "Unvested": st.column_config.NumberColumn(format="localized"),
                           "Vests at cliff": st.column_config.NumberColumn(format="localized")},
        )

        st.markdown("**Vesting schedule**")
        who = st.selectbox("Founder", [f["name"] for f in cap["founders"]], key="sched_founder")
        f = next(x for x in cap["founders"] if x["name"] == who)
        st.dataframe(
            [{"Month": r["month"], "Date": r["date"], "Vests this month": r["vested_this_month"],
              "Total vested": r["cumulative_vested"], "Unvested": r["unvested"],
              "% vested": r["percent_vested"]} for r in f.get("schedule", [])],
            hide_index=True, width="stretch", height=320,
            column_config={"% vested": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f%%"),
                           "Vests this month": st.column_config.NumberColumn(format="localized"),
                           "Total vested": st.column_config.NumberColumn(format="localized"),
                           "Unvested": st.column_config.NumberColumn(format="localized")},
        )

        d1, d2, d3, _ = st.columns(4)
        for col, (fmt, label, fname) in zip((d1, d2, d3), [
            ("csv", "Download CSV", "cap_table.csv"),
            ("xlsx", "Download Excel", "cap_table.xlsx"),
            ("vesting_csv", "Vesting schedule CSV", "vesting_schedule.csv"),
        ]):
            data = call("GET", f"/cases/{case_id}/cap-table/download", params={"format": fmt}).content
            col.download_button(label, data, file_name=f"{case['name']}-{fname}", key=f"dl_{fmt}",
                                width="stretch", type="primary" if fmt == "xlsx" else "secondary")


# ---------- Tab 3: Document Review & Sign-Off ----------

with tab_docs:
    docs = case["documents"]
    if not docs:
        st.info("No documents yet. Finish the chat with the Manager Agent first.")
    else:
        doc_ids = [d["id"] for d in docs]
        by_id = {d["id"]: d for d in docs}
        sel = st.selectbox("Document", doc_ids, key="doc_select",
                           format_func=lambda i: f"{by_id[i]['doc_type']}  ·  v{by_id[i]['version']}  ·  "
                                                 f"{by_id[i]['status'].replace('_', ' ')}")
        doc = by_id[sel]
        inval = last_invalidation(doc, case["audit_logs"])
        invalidated = set(inval.get("invalidated_signatures", [])) if inval else set()

        h1, h2 = st.columns([3, 2])
        h1.markdown(f"### {doc['doc_type']}")
        h2.markdown(f"**Version {doc['version']}** &nbsp; {badge(doc['status'])}")

        # Who has signed
        st.markdown("**Signatures**")
        cols = st.columns(max(len(doc["approvals"]), 1))
        for col, (name, signed_v) in zip(cols, doc["approvals"].items()):
            you = " (you)" if name == active_user else ""
            if signed_v == doc["version"]:
                col.success(f"**{name}**{you}  \nSigned v{signed_v}", icon="✅")
            elif name in invalidated:
                col.error(f"**{name}**{you}  \nSignature invalidated by {inval['edited_by']}'s edit "
                          f"({inval['version'].replace('->', '→')})", icon="❌")
            else:
                col.info(f"**{name}**{you}  \nNot signed yet", icon="⏳")

        already = doc["approvals"].get(active_user) == doc["version"]
        if doc["status"] == "EXECUTED":
            st.success("All founders have signed this version. The document is **EXECUTED**.")
        approve_label = f"Approve as {active_user}" if active_user else "Approve"
        if st.button(approve_label, type="primary", key=f"approve_{doc['id']}_{doc['version']}",
                     disabled=not active_user or already or doc["status"] == "EXECUTED",
                     help="You already signed this version." if already else None):
            d = api("POST", f"/documents/{doc['id']}/approve",
                    json={"founder_name": active_user, "version": doc["version"]})
            if d:
                flash(f"{active_user} signed {doc['doc_type']} v{doc['version']}."
                      + (" Every founder has signed: **EXECUTED**." if d["status"] == "EXECUTED" else ""))
                st.rerun()

        read_col, edit_col = st.columns([3, 2], gap="large")
        with read_col:
            st.markdown("**Document**")
            with st.container(height=520, border=True):
                st.markdown(doc["content_md"])

        with edit_col:
            st.markdown("**Edit Clause**")
            clauses = split_clauses(doc["content_md"])
            idx = st.selectbox("Clause", range(len(clauses)), format_func=lambda i: clauses[i][0],
                               key=f"clause_{doc['id']}_{doc['version']}")
            new_text = st.text_area("Clause text (Markdown)", clauses[idx][1], height=300,
                                    key=f"clause_text_{doc['id']}_{doc['version']}_{idx}")
            others = [n for n, v in doc["approvals"].items() if v is not None and n != active_user]
            st.caption(f"Saving creates **v{doc['version'] + 1}** as {active_user or '—'}. "
                       + (f"This invalidates the signature of **{', '.join(others)}**." if others
                          else "Every other founder will need to sign the new version."))
            if st.button(f"Save as v{doc['version'] + 1}", key=f"save_{doc['id']}_{doc['version']}",
                         disabled=not active_user):
                original = clauses[idx][1]
                if original.endswith("\n") and not new_text.endswith("\n"):
                    new_text += "\n"
                parts = [text for _, text in clauses]
                parts[idx] = new_text
                r = api("POST", f"/documents/{doc['id']}/edit",
                        json={"founder_name": active_user, "content_md": "".join(parts),
                              "base_version": doc["version"]})
                if r:
                    lost = r["invalidated_signatures"]
                    flash(f"{active_user} edited \"{clauses[idx][0]}\": {doc['doc_type']} is now "
                          f"**v{r['document']['version']}**."
                          + (f" Signature invalidated: **{', '.join(lost)}**." if lost else ""),
                          "warning" if lost else "success")
                    st.rerun()


# ---------- Tab 4: Audit Trail ----------

EVENT_TYPES = {
    "Approvals": {"document_approved"},
    "Edits": {"document_edited"},
    "Signature invalidations": {"signature_invalidated"},
    "Status changes": {"status_changed"},
    "Agents & drafting": {"terms_locked", "needs_info", "document_drafted", "cap_table_built"},
    "Chat & case": {"chat_message", "case_created", "founder_added"},
}
EVENT_LABEL = {
    "document_approved": "✅ Approved", "document_edited": "✏️ Edited",
    "signature_invalidated": "❌ Signature invalidated", "status_changed": "🔁 Status changed",
    "terms_locked": "🔒 Terms locked", "needs_info": "❓ Needs info", "document_drafted": "📄 Drafted",
    "cap_table_built": "📊 Cap table built", "chat_message": "💬 Chat message",
    "case_created": "🆕 Case created", "founder_added": "👤 Founder added",
}


def audit_rows(audit: list[dict]) -> list[dict]:
    """Oldest first. Each edit also yields one 'signature invalidated' row per affected founder."""
    rows = []
    for a in reversed(audit):
        ts = a["timestamp"].replace("T", " ")[:19]
        if a["action"] == "document_edited":
            try:
                e = json.loads(a["details"])
            except ValueError:
                e = {}
            lost = e.get("invalidated_signatures", [])
            rows.append({"Time (UTC)": ts, "Actor": a["actor"], "Event": a["action"],
                         "Details": f"{e.get('document')} {e.get('version', '').replace('->', '→')}"
                                    + (f" (invalidated: {', '.join(lost)})" if lost else "")})
            for name in lost:
                rows.append({"Time (UTC)": ts, "Actor": name, "Event": "signature_invalidated",
                             "Details": f"{name}'s signature on {e.get('document')} "
                                        f"{e.get('version', '').split('->')[0].strip()} invalidated by "
                                        f"{e.get('edited_by')}'s edit"})
        else:
            rows.append({"Time (UTC)": ts, "Actor": a["actor"], "Event": a["action"], "Details": a["details"]})
    return rows


with tab_audit:
    picked = st.multiselect("Show", list(EVENT_TYPES), default=["Approvals", "Edits", "Signature invalidations",
                                                                 "Status changes"], key="audit_filter")
    wanted = set().union(*(EVENT_TYPES[p] for p in picked)) if picked else set()
    rows = [r for r in audit_rows(case["audit_logs"]) if r["Event"] in wanted]
    for r in rows:
        r["Event"] = EVENT_LABEL.get(r["Event"], r["Event"])
    st.caption(f"{len(rows)} events, oldest first. Every approval, edit and status change is recorded by the API.")
    st.dataframe(rows, hide_index=True, width="stretch", height=520,
                 column_config={"Details": st.column_config.TextColumn(width="large")})
