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


def api(method: str, path: str, **kwargs):
    try:
        r = requests.request(method, f"{API}{path}", timeout=300, **kwargs)
    except requests.ConnectionError:
        st.error(f"Cannot reach API at {API}. Start it with `uvicorn app.main:app --reload`.")
        st.stop()
    if not r.ok:
        st.error(f"{r.status_code}: {r.text}")
        return None
    return r.json()


# --- Sidebar: create / select case ---
with st.sidebar:
    st.header("New case")
    with st.form("new_case", clear_on_submit=True):
        name = st.text_input("Company name")
        entity_type = st.selectbox("Entity type", ["C-Corp", "LLC", "S-Corp"])
        state = st.text_input("State", "Delaware")
        inc_date = st.date_input("Incorporation date", value=None)
        if st.form_submit_button("Create") and name:
            api("POST", "/cases", json={
                "name": name, "entity_type": entity_type, "state": state,
                "incorporation_date": inc_date.isoformat() if inc_date else None,
            })
            st.rerun()

    cases = api("GET", "/cases") or []
    if not cases:
        st.info("Create a case to get started.")
        st.stop()
    labels = {f"#{c['id']} {c['name']} ({c['status']})": c["id"] for c in cases}
    case_id = labels[st.selectbox("Select case", list(labels))]

case = api("GET", f"/cases/{case_id}")
st.subheader(f"{case['name']} — {case['entity_type']}, {case['state']}")
st.caption(f"Status: **{case['status']}**")

tab_founders, tab_docs, tab_audit = st.tabs(["Founders", "Documents", "Audit log"])

with tab_founders:
    if case["founders"]:
        st.dataframe(case["founders"], width="stretch")
        total = sum(f["equity_percent"] for f in case["founders"])
        st.write(f"Total equity: **{total:.2f}%**")
    with st.form("add_founder", clear_on_submit=True):
        c1, c2 = st.columns(2)
        f_name = c1.text_input("Name")
        f_email = c2.text_input("Email")
        c3, c4, c5 = st.columns(3)
        equity = c3.number_input("Equity %", 0.01, 100.0, 50.0)
        vesting = c4.number_input("Vesting months", 0, 120, 48)
        cliff = c5.number_input("Cliff months", 0, 60, 12)
        if st.form_submit_button("Add founder") and f_name and f_email:
            api("POST", f"/cases/{case_id}/founders", json={
                "name": f_name, "email": f_email, "equity_percent": equity,
                "vesting_months": vesting, "cliff_months": cliff,
            })
            st.rerun()

    if st.button("Run compliance check & draft documents", type="primary"):
        with st.spinner("Running workflow (DeepSeek drafting may take a minute)..."):
            result = api("POST", f"/cases/{case_id}/run")
        if result:
            if result["issues"]:
                st.warning("Blocked by compliance issues:\n\n" + "\n".join(f"- {i}" for i in result["issues"]))
            else:
                st.success(f"Drafted: {', '.join(result['documents'])}")

with tab_docs:
    docs = api("GET", f"/cases/{case_id}/documents") or []
    if not docs:
        st.info("No documents yet. Run the workflow from the Founders tab.")
    for doc in docs:
        approvals = json.loads(doc["approvals_json"])
        with st.expander(f"{doc['doc_type']} v{doc['version']} — {doc['status']} ({len(approvals)} approvals)"):
            st.markdown(doc["content_md"])
            emails = [f["email"] for f in case["founders"] if f["email"] not in approvals]
            if emails and doc["status"] != "approved":
                approver = st.selectbox("Approve as", emails, key=f"appr_{doc['id']}")
                if st.button("Approve", key=f"btn_{doc['id']}"):
                    api("POST", f"/documents/{doc['id']}/approve", json={"approver": approver})
                    st.rerun()

with tab_audit:
    st.dataframe(api("GET", f"/cases/{case_id}/audit") or [], width="stretch")
