"""
PharmaNexus Lead Agent: web dashboard (Streamlit).
All the work is done by lead_agent.py. Leads are stored in a Supabase table.
Passwords and keys live in the Streamlit app "Secrets", never in this file.
"""
import contextlib
import csv
import datetime as dt
import hmac
import io
import os
from collections import Counter

import pandas as pd
import requests
import streamlit as st

import lead_agent as la

st.set_page_config(page_title="Lead Agent", page_icon="📬", layout="wide")

STATUSES = ["New", "Low fit", "Qualified", "Drafted", "Approved",
            "Sent", "Replied", "Signed up", "Not interested"]
EDITABLE = ["status", "subject", "draft", "date_sent", "notes"]


def secret(name):
    try:
        return str(st.secrets[name])
    except Exception:
        return ""


# ---------- login ----------
def require_login():
    password = secret("APP_PASSWORD")
    if not password:
        st.error("APP_PASSWORD is missing in the app Secrets. Add it, then reboot the app.")
        st.stop()
    if st.session_state.get("logged_in"):
        return
    st.title("📬 Lead Agent")
    entered = st.text_input("Password", type="password")
    if entered:
        if hmac.compare_digest(entered.encode(), password.encode()):
            st.session_state["logged_in"] = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()


require_login()

for name in ("GEMINI_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
    if secret(name):
        os.environ[name] = secret(name)

SB_URL = secret("SUPABASE_URL").rstrip("/")
SB_KEY = secret("SUPABASE_KEY")
if not SB_URL or not SB_KEY:
    st.error("SUPABASE_URL and SUPABASE_KEY are missing in the app Secrets.")
    st.stop()

# ---------- database (replaces the CSV file used on your laptop) ----------
_saved = {}


def _headers(prefer=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    return h


def _norm(lead):
    return {k: lead.get(k) or "" for k in la.FIELDS}


def db_load():
    rows, offset = [], 0
    while True:
        r = requests.get(f"{SB_URL}/rest/v1/leads", headers=_headers(), timeout=30, params={
            "select": "*", "order": "created_at.asc,email.asc", "limit": 1000, "offset": offset})
        if r.status_code != 200:
            raise RuntimeError(f"Database error {r.status_code}: {r.text[:200]}")
        batch = r.json()
        rows += batch
        if len(batch) < 1000:
            break
        offset += 1000
    leads = [_norm(x) for x in rows]
    _saved.clear()
    _saved.update({l["email"]: dict(l) for l in leads})
    return leads


def db_save(leads):
    changed = [_norm(l) for l in leads if _norm(l) != _saved.get(l["email"])]
    for i in range(0, len(changed), 200):
        chunk = changed[i:i + 200]
        r = requests.post(f"{SB_URL}/rest/v1/leads", params={"on_conflict": "email"}, json=chunk,
                          headers=_headers("resolution=merge-duplicates,return=minimal"), timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"Database error {r.status_code}: {r.text[:200]}")
        for l in chunk:
            _saved[l["email"]] = dict(l)


la.load, la.save = db_load, db_save  # lead_agent.py now reads and writes the database


# ---------- helpers ----------
def run(label, fn, *args):
    buf = io.StringIO()
    with st.spinner(f"{label}... keep this page open"):
        try:
            with contextlib.redirect_stdout(buf):
                fn(*args)
        except SystemExit as e:
            buf.write(f"\nStopped: {e}\n")
        except Exception as e:
            buf.write(f"\nSomething went wrong: {e}\n")
    st.session_state["log"] = buf.getvalue().strip() or "Done."
    st.rerun()


def import_rows(text):
    leads = db_load()
    have = {l["email"] for l in leads}
    added = 0
    for row in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip().lower(): (v or "") for k, v in row.items()}
        email = row.get("email", "").strip().lower()
        if not email or email in have:
            continue
        lead = {k: row.get(k, "") for k in la.FIELDS}
        lead["email"] = email
        lead["status"] = lead["status"] or "New"
        leads.append(lead)
        have.add(email)
        added += 1
    db_save(leads)
    print(f"Imported {added} new leads")


def to_csv(leads):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=la.FIELDS)
    w.writeheader()
    w.writerows(leads)
    return out.getvalue().encode("utf-8-sig")


# ---------- page ----------
st.title("📬 Lead Agent")
log = st.session_state.pop("log", None)
if log:
    st.info("Last action")
    st.code(log)

try:
    leads = db_load()
except Exception as e:
    st.error(f"Could not read the database: {e}")
    st.stop()

tab_over, tab_run, tab_leads = st.tabs(["Overview", "Run", "Leads"])

with tab_over:
    counts = Counter(l["status"] or "New" for l in leads)
    c = st.columns(4)
    c[0].metric("Total leads", len(leads))
    c[1].metric("Approved, ready to send", counts["Approved"])
    c[2].metric("Sent", counts["Sent"])
    c[3].metric("Replied or signed up", counts["Replied"] + counts["Signed up"])
    if leads:
        st.bar_chart(pd.Series({s: counts.get(s, 0) for s in STATUSES}))
    st.subheader(f"Follow-ups due (sent {la.FOLLOWUP_AFTER_DAYS}+ days ago, no reply)")
    today = dt.date.today()
    due = [l for l in leads if l["status"] == "Sent" and la.parse_date(l["date_sent"])
           and (today - la.parse_date(l["date_sent"])).days >= la.FOLLOWUP_AFTER_DAYS]
    if due:
        st.dataframe(pd.DataFrame(due, columns=["name", "email", "date_sent", "subject"]))
    else:
        st.write("None right now.")
    st.download_button("Download all leads (CSV backup)", data=to_csv(leads),
                       file_name="leads_tracker.csv", mime="text/csv")

with tab_run:
    st.write("Each step works in small batches so it finishes quickly. Click again for the next batch.")
    left, right = st.columns(2)
    with left:
        st.subheader("1. Find")
        if st.button("Search PubMed for new leads", key="find"):
            run("Searching PubMed", la.find_pubmed)
        up = st.file_uploader("Or import a CSV (your old leads_tracker.csv works too)", type="csv", key="upload")
        if up is not None and st.button("Import this file", key="import"):
            run("Importing", import_rows, up.getvalue().decode("utf-8-sig"))
        st.subheader("2. Score")
        n_score = st.number_input("How many leads to score now", 1, 20, 10, key="n_score")
        if st.button("Score next leads", key="score"):
            run("Scoring", la.qualify, int(n_score))
    with right:
        st.subheader("3. Write")
        n_draft = st.number_input("How many drafts to write now", 1, 20, 5, key="n_draft")
        if st.button("Write next drafts", key="draft"):
            run("Writing drafts", la.draft, int(n_draft))
        st.subheader("4. Send")
        st.caption("Only leads with status Approved are sent. Approve drafts in the Leads tab first. "
                   "There is a 1-3 minute pause between emails, so large batches take a while.")
        n_send = st.number_input("How many emails to send now", 1, 5, 3, key="n_send")
        if st.button("Send approved emails", key="send"):
            run("Sending", la.send_emails, False, int(n_send))
        if st.button("Send a test email to myself", key="test"):
            run("Sending test email", la.send_emails, True)

with tab_leads:
    pick = st.multiselect("Show only these statuses", STATUSES)
    view = [l for l in leads if not pick or l["status"] in pick]
    st.caption("Edit the subject or draft text directly. To approve an email, set its status to Approved. "
               "After sending by hand, set it to Sent and type the date as 2026-11-20. Then click Save.")
    df = pd.DataFrame(view, columns=la.FIELDS)
    edited = st.data_editor(
        df, hide_index=True, num_rows="fixed", key="grid",
        disabled=[col for col in la.FIELDS if col not in EDITABLE],
        column_config={"status": st.column_config.SelectboxColumn("status", options=STATUSES, required=True)})
    if st.button("Save changes", key="save"):
        by_email = {l["email"]: l for l in leads}
        for row in edited.to_dict("records"):
            lead = by_email[row["email"]]
            for col in EDITABLE:
                value = row[col]
                lead[col] = "" if pd.isna(value) else str(value)
        db_save(leads)
        st.session_state["log"] = "Changes saved."
        st.rerun()
