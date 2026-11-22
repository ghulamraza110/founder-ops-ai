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


# --- Sidebar: create / select case ---
with st.sidebar:
    if st.button("New case from chat", type="primary", width="stretch"):
        new = api("POST", "/cases", json={})
        if new:
            st.session_state["case_id"] = new["id"]
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
    if not cases:
        st.info("Start a case to get started.")
        st.stop()
    ids = [c["id"] for c in cases]
    labels = {c["id"]: f"#{c['id']} {c['name']} ({c['status']})" for c in cases}
    current = st.session_state.get("case_id")
    case_id = st.selectbox("Select case", ids, index=ids.index(current) if current in ids else 0,
                           format_func=labels.get)
    st.session_state["case_id"] = case_id

case = api("GET", f"/cases/{case_id}")
st.subheader(f"{case['name']} — {case['entity_type']}, {case['state']}")
meta = f"Status: **{case['status']}**"
if case["incorporation_date"]:
    meta += f" · Incorporated {case['incorporation_date']}"
st.caption(meta)


def show_result(result: dict) -> None:
    if result["terms_ready"]:
        st.success(f"Terms locked. IRS 83(b) deadline: **{result['deadline_83b']}**. "
                   f"Drafted: {', '.join(result['documents'])}. See the Documents and Cap table tabs.")
    elif result["questions"] and not result.get("reply"):
        st.warning("Missing or invalid terms:\n\n" + "\n".join(f"- {q}" for q in result["questions"]))


tab_chat, tab_founders, tab_docs, tab_cap, tab_audit = st.tabs(
    ["Chat", "Founders", "Documents", "Cap table", "Audit log"])

with tab_chat:
    st.caption("Describe your startup: company name, LLC or Delaware C-Corp, founders and their equity, "
               "vesting, and incorporation date. The Manager Agent asks for anything missing, then drafts "
               "documents and builds the cap table.")
    for m in api("GET", f"/cases/{case_id}/chat") or []:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
    if prompt := st.chat_input("Message the Manager Agent"):
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.spinner("Agents working (drafting can take ~15s)..."):
            result = api("POST", f"/cases/{case_id}/chat", json={"message": prompt})
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
        f_email = c2.text_input("Email")
        c3, c4, c5 = st.columns(3)
        equity = c3.number_input("Equity %", 0.01, 100.0, 50.0)
        vesting = c4.number_input("Vesting months", 1, 120, 48)
        cliff = c5.number_input("Cliff months", 0, 60, 12)
        if st.form_submit_button("Add founder") and f_name:
            api("POST", f"/cases/{case_id}/founders", json={
                "name": f_name, "email": f_email, "equity_percent": equity,
                "vesting_months": vesting, "cliff_months": cliff,
            })
            st.rerun()

    if st.button("Validate & run builders", type="primary"):
        with st.spinner("Drafting documents and building the cap table..."):
            result = api("POST", f"/cases/{case_id}/run")
        if result:
            show_result(result)

with tab_docs:
    docs = api("GET", f"/cases/{case_id}/documents") or []
    if not docs:
        st.info("No documents yet. Finish the chat or run the builders from the Founders tab.")
    founder_ids = [f["email"] or f["name"] for f in case["founders"]]
    for doc in docs:
        approvals = json.loads(doc["approvals_json"])
        with st.expander(f"{doc['doc_type']} v{doc['version']} — {doc['status']} "
                         f"({len(approvals)}/{len(founder_ids)} approvals)"):
            st.markdown(doc["content_md"])
            pending = [f for f in founder_ids if f not in approvals]
            if pending and doc["status"] != "approved":
                approver = st.selectbox("Approve as", pending, key=f"appr_{doc['id']}")
                if st.button("Approve", key=f"btn_{doc['id']}"):
                    api("POST", f"/documents/{doc['id']}/approve", json={"approver": approver})
                    st.rerun()

with tab_cap:
    r = call("GET", f"/cases/{case_id}/cap-table")
    if r.status_code == 404:
        st.info("No cap table yet. Finish the chat or run the builders from the Founders tab.")
    elif r.ok:
        cap = r.json()
        unit = cap["unit"]
        c1, c2, c3 = st.columns(3)
        c1.metric(f"Authorized {unit}", f"{cap['authorized_shares']:,}")
        c2.metric(f"Issued {unit}", f"{cap['issued_shares']:,}")
        c3.metric("Founders", len(cap["founders"]))
        st.dataframe(
            [{"Founder": f["name"], "Equity %": f["equity_percent"], unit.title(): f"{f['shares']:,}",
              "Vesting": f"{f['vesting_months']}m", "Cliff": f"{f['cliff_months']}m",
              "Cliff date": f["cliff_date"], f"Vested at cliff": f"{f['shares_vested_at_cliff']:,}",
              "Fully vested": f["fully_vested_date"]} for f in cap["founders"]],
            width="stretch", hide_index=True,
        )
    else:
        st.error(f"{r.status_code}: {r.text}")

    files = api("GET", f"/cases/{case_id}/exports") or []
    if files:
        st.markdown("**Downloads**")
        cols = st.columns(min(len(files), 4))
        for i, name in enumerate(files):
            data = call("GET", f"/cases/{case_id}/exports/{name}").content
            cols[i % len(cols)].download_button(name, data, file_name=name, key=f"dl_{name}", width="stretch")

with tab_audit:
    st.dataframe(api("GET", f"/cases/{case_id}/audit") or [], width="stretch",
                 column_order=["timestamp", "actor", "action", "details"])
