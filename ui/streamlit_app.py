"""Streamlit UI. Run: streamlit run ui/streamlit_app.py (with the API running)."""
import json
import os

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
API = os.getenv("API_URL", "http://localhost:8000")

st.set_page_config(page_title="Founder Ops AI", layout="wide")
st.title("Startup Incorporation & Compliance Engine")

STATUS_BADGE = {
    "DRAFT": ":gray[DRAFT]",
    "PENDING_APPROVAL": ":orange[PENDING APPROVAL]",
    "INVALIDATED": ":red[INVALIDATED]",
    "EXECUTED": ":green[EXECUTED]",
}


def call(method: str, path: str, **kwargs) -> requests.Response:
    try:
        return requests.request(method, f"{API}{path}", timeout=300, **kwargs)
    except requests.ConnectionError:
        st.error(f"Cannot reach API at {API}. Start it with `uvicorn app.main:app --reload`.")
        st.stop()


def api(method: str, path: str, **kwargs):
    r = call(method, path, **kwargs)
    if not r.ok:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        st.error(f"{r.status_code}: {detail}")
        return None
    return r.json()


def flash(msg: str, kind: str = "success") -> None:
    """Show a message after the next rerun."""
    st.session_state["flash"] = (kind, msg)


# --- Sidebar: create / select case ---
with st.sidebar:
    if st.button("New case from chat", type="primary", width="stretch"):
        st.session_state["case_id"] = None
        st.rerun()

    with st.expander("New case from form"):
        with st.form("new_case", clear_on_submit=True):
            name = st.text_input("Company name")
            entity_type = st.selectbox("Entity type", ["Delaware C-Corp", "LLC"])
            state = st.text_input("State (LLC only)", "Delaware")
            inc_date = st.date_input("Incorporation date", value=None)
            if st.form_submit_button("Create") and name:
                new = api("POST", "/cases", json={
                    "name": name, "entity_type": entity_type,
                    "state": "Delaware" if entity_type == "Delaware C-Corp" else state,
                    "incorporation_date": inc_date.isoformat() if inc_date else None,
                })
                if new:
                    st.session_state["case_id"] = new["id"]
                    st.rerun()

    cases = api("GET", "/cases") or []
    if cases:
        ids = [c["id"] for c in cases]
        labels = {c["id"]: f"#{c['id']} {c['name']} ({c['status']})" for c in cases}
        current = st.session_state.get("case_id", ids[0])
        options = [None, *ids]
        case_id = st.selectbox("Select case", options, index=options.index(current) if current in options else 1,
                               format_func=lambda i: "➕ New chat" if i is None else labels[i])
        st.session_state["case_id"] = case_id
    else:
        case_id = st.session_state["case_id"] = None

if kind_msg := st.session_state.pop("flash", None):
    getattr(st, kind_msg[0])(kind_msg[1])

# --- New chat (no case yet) ---
if case_id is None:
    st.subheader("New case")
    st.caption("Describe your startup: company name, LLC or Delaware C-Corp, founders and their equity, "
               "vesting, and incorporation date. The Manager Agent asks for anything missing, then drafts "
               "documents and builds the cap table.")
    if prompt := st.chat_input("Message the Manager Agent"):
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.spinner("Agents working..."):
            result = api("POST", "/cases/chat", json={"message": prompt})
        if result:
            st.session_state["case_id"] = result["case_id"]
            st.rerun()
    st.stop()

case = api("GET", f"/cases/{case_id}")
if not case:
    st.stop()
st.subheader(f"{case['name']} — {case['entity_type']}, {case['state']}")
meta = f"Status: **{case['status']}**"
if case["incorporation_date"]:
    meta += f" · Incorporated {case['incorporation_date']}"
if case["deadline_83b"]:
    meta += f" · IRS 83(b) deadline **{case['deadline_83b']}**"
st.caption(meta)

tab_chat, tab_founders, tab_docs, tab_cap, tab_audit = st.tabs(
    ["Chat", "Founders", "Documents", "Cap table", "Audit log"])

with tab_chat:
    for m in api("GET", f"/cases/{case_id}/chat") or []:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
    if prompt := st.chat_input("Message the Manager Agent"):
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.spinner("Agents working (drafting can take ~15s)..."):
            result = api("POST", "/cases/chat", json={"case_id": case_id, "message": prompt})
        if result:
            st.rerun()

with tab_founders:
    if case["founders"]:
        st.dataframe(case["founders"], width="stretch",
                     column_order=["name", "email", "equity_percent", "vesting_months", "cliff_months"])
        total = sum(f["equity_percent"] for f in case["founders"])
        st.write(f"Total equity: **{total:.2f}%**")
    else:
        st.info("No founders yet. Add them here, or describe them in the Chat tab.")
    with st.form("add_founder", clear_on_submit=True):
        c1, c2 = st.columns(2)
        f_name = c1.text_input("Name")
        f_email = c2.text_input("Email (optional)")
        c3, c4, c5 = st.columns(3)
        equity = c3.number_input("Equity %", 0.01, 100.0, 50.0)
        vesting = c4.number_input("Vesting months", 1, 120, 48)
        cliff = c5.number_input("Cliff months", 0, 60, 12)
        if st.form_submit_button("Add founder") and f_name:
            if api("POST", f"/cases/{case_id}/founders", json={
                "name": f_name, "email": f_email, "equity_percent": equity,
                "vesting_months": vesting, "cliff_months": cliff,
            }):
                st.rerun()

    if st.button("Validate & run builders", type="primary"):
        with st.spinner("Drafting documents and building the cap table..."):
            result = api("POST", f"/cases/{case_id}/run")
        if result:
            if result["terms_ready"]:
                flash(f"Drafted: {', '.join(result['documents'])}. See the Documents and Cap table tabs.")
                st.rerun()
            st.warning("Missing or invalid terms:\n\n" + "\n".join(f"- {q}" for q in result["questions"]))

with tab_docs:
    founders = [f["name"] for f in case["founders"]]
    if not case["documents"]:
        st.info("No documents yet. Finish the chat or run the builders from the Founders tab.")
    for doc in case["documents"]:
        signed = sum(v == doc["version"] for v in doc["approvals"].values())
        with st.expander(f"{doc['doc_type']} · v{doc['version']} · {doc['status'].replace('_', ' ')} "
                         f"· {signed}/{len(doc['approvals'])} signed"):
            st.markdown(f"Status: {STATUS_BADGE.get(doc['status'], doc['status'])}")
            st.markdown(" · ".join(
                f"{'✅' if v == doc['version'] else '⬜'} {name}" + (f" (signed v{v})" if v and v != doc['version'] else "")
                for name, v in doc["approvals"].items()
            ))

            view, edit = st.tabs(["Read & sign", "Edit (new version)"])
            with view:
                st.markdown(doc["content_md"])
                if doc["pending"] and doc["status"] != "EXECUTED":
                    c1, c2 = st.columns([3, 1])
                    who = c1.selectbox("Sign as", doc["pending"], key=f"appr_{doc['id']}")
                    if c2.button("Approve", key=f"btn_{doc['id']}", width="stretch"):
                        d = api("POST", f"/documents/{doc['id']}/approve", json={"founder_name": who})
                        if d:
                            flash(f"{who} signed {doc['doc_type']} v{doc['version']}."
                                  + (" All founders have signed: EXECUTED." if d["status"] == "EXECUTED" else ""))
                            st.rerun()
            with edit:
                st.caption("Saving creates a new version. Every other founder's signature is reset, "
                           "and the edit is recorded in the audit log.")
                editor = st.selectbox("Editing as", founders, key=f"editor_{doc['id']}")
                new_md = st.text_area("Markdown", doc["content_md"], height=400, key=f"md_{doc['id']}")
                if st.button("Save new version", key=f"save_{doc['id']}"):
                    r = api("POST", f"/documents/{doc['id']}/edit",
                            json={"founder_name": editor, "content_md": new_md})
                    if r:
                        lost = r["invalidated_signatures"]
                        flash(f"{doc['doc_type']} is now v{r['document']['version']}."
                              + (f" Signatures invalidated: {', '.join(lost)}." if lost else ""), "warning")
                        st.rerun()

with tab_cap:
    cap = case["cap_table"]
    if not cap:
        st.info("No cap table yet. Finish the chat or run the builders from the Founders tab.")
    else:
        unit = cap["unit"]
        c1, c2, c3 = st.columns(3)
        c1.metric(f"Authorized {unit}", f"{cap['authorized_shares']:,}")
        c2.metric(f"Issued {unit}", f"{cap['issued_shares']:,}")
        c3.metric("Founders", len(cap["founders"]))
        st.dataframe(
            [{"Founder": f["name"], "Equity %": f["equity_percent"], unit.title(): f"{f['shares']:,}",
              "Vesting": f"{f['vesting_months']}m", "Cliff": f"{f['cliff_months']}m",
              "Cliff date": f["cliff_date"], "Vested at cliff": f"{f['shares_vested_at_cliff']:,}",
              "Fully vested": f["fully_vested_date"]} for f in cap["founders"]],
            width="stretch", hide_index=True,
        )
        st.markdown("**Download**")
        cols = st.columns(4)
        for col, (fmt, label, fname) in zip(cols, [
            ("xlsx", "Excel", "cap_table.xlsx"), ("csv", "Cap table CSV", "cap_table.csv"),
            ("vesting_csv", "Vesting CSV", "vesting_schedule.csv"), ("json", "JSON", "cap_table.json"),
        ]):
            data = call("GET", f"/cases/{case_id}/cap-table/download", params={"format": fmt}).content
            col.download_button(label, data, file_name=f"case-{case_id}-{fname}", key=f"dl_{fmt}", width="stretch")

with tab_audit:
    st.dataframe(case["audit_logs"], width="stretch", column_order=["timestamp", "actor", "action", "details"])
