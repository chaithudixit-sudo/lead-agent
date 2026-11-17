"""
LEAD AGENT: Find -> Qualify -> Write -> Track

Run these in your terminal, from the folder where this file is saved:

  python lead_agent.py find              # search PubMed for researchers with public emails
  python lead_agent.py import FILE.csv   # add your own list (columns: name,email,affiliation,source)
  python lead_agent.py qualify           # AI scores new leads from 1 to 5
  python lead_agent.py draft             # AI writes emails for good leads -> drafts.txt
  python lead_agent.py send test         # send ONE test email to yourself (Gmail)
  python lead_agent.py send              # send the drafts you marked Approved (Gmail)
  python lead_agent.py report            # status counts + who needs a follow-up
  python lead_agent.py all               # find + qualify + draft in one go

Emails are only sent for leads whose status you changed to Approved.
"""
import csv
import datetime as dt
import json
import os
import random
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from email.message import EmailMessage

import requests

# =====================================================================
# 1. EDIT THIS SECTION (the only part you need to change)
# =====================================================================
APP_NAME = "PharmaNexus QbD"
APP_URL = "https://qbd.pharmanexus.in"
APP_DESCRIPTION = "A web platform for analytical Quality by Design (QbD). It helps analysts plan and optimize HPLC and dissolution methods using design of experiments (Box-Behnken, response surface), supports ICH Q2(R2) method validation calculations and ICH Q14 profile comparison, and includes stability modelling, green chemistry metrics, an audit trail and an AI copilot. It is currently open for beta testing."
SENDER_NAME = "PharmaNexus.in"
SENDER_ROLE = "Analytical QbD platform for pharma scientists"

# Who benefits most from your app? The AI uses this to score leads.
IDEAL_CUSTOMER = """
Best fit: analytical scientists and method-development researchers working on
HPLC, dissolution or stability methods (PhD scholars, M.Pharm students, faculty,
and analytical R&D / QC staff in pharma companies and labs) who use or want to
learn design of experiments, ICH Q14 and ICH Q2(R2) method validation.
Weaker fit: people in unrelated fields such as clinical, marketing or
formulation-only work with no analytical component.
"""

# PubMed search words. Each line is one search.
PUBMED_QUERIES = [
    "analytical quality by design HPLC",
    "Box-Behnken design HPLC method development",
    "dissolution method development validation",
    "ICH Q14 analytical procedure development",
    "ICH Q2(R2) method validation",
    "green analytical chemistry method",
]
PUBMED_FROM_YEAR = 2025      # only papers from this year onward
MAX_PAPERS_PER_QUERY = 50    # more papers = more leads, but slower

# What you want each type of lead to do
CTA_BY_SEGMENT = {
    "Researcher/Student": "invite them to try the beta and share feedback",
    "Company/Lab": "offer a short demo call and invite them to try the beta",
    "College/Institute": "offer a demo for their department and invite faculty to try the beta",
    "Other": "invite them to visit the website",
}

MODEL = "gemini-3.5-flash-lite"   # if you get a "model not found" error, try "gemini-3.6-flash"
SECONDS_BETWEEN_CALLS = 6         # keeps you inside the free-tier speed limit
MIN_SCORE_TO_DRAFT = 3        # leads scoring below this are marked "Low fit"
FOLLOWUP_AFTER_DAYS = 7       # report shows "Sent" leads older than this
MAX_EMAILS_PER_DAY = 20       # safety cap for sending (keep it low at first)
SEND_DELAY_SECONDS = (60, 180)  # random pause between emails, in seconds
# =====================================================================

TRACKER = "leads_tracker.csv"
DRAFTS = "drafts.txt"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
FIELDS = ["email", "name", "affiliation", "source", "segment", "score",
          "reason", "subject", "draft", "status", "date_sent", "notes"]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


# ---------- tracker file helpers ----------
def load():
    if not os.path.exists(TRACKER):
        return []
    with open(TRACKER, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [{k: (r.get(k) or "") for k in FIELDS} for r in rows]


def save(leads):
    try:
        with open(TRACKER, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(leads)
    except PermissionError:
        sys.exit(f"Please close {TRACKER} (e.g. in Excel) and run the command again.")


def add_lead(leads, email, name, affiliation, source):
    email = email.strip().lower()
    if not email or any(l["email"] == email for l in leads):
        return False
    leads.append({"email": email, "name": name.strip(), "affiliation": affiliation[:300],
                  "source": source[:200], "status": "New"})
    return True


def parse_date(text):
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    return None


# ---------- FIND ----------
def find_pubmed():
    if "keyword one" in PUBMED_QUERIES:
        sys.exit("Please edit PUBMED_QUERIES at the top of the file first.")
    leads = load()
    added = 0
    for q in PUBMED_QUERIES:
        print(f"Searching PubMed: {q}")
        term = f"({q}) AND ({PUBMED_FROM_YEAR}:3000[dp])"
        r = requests.get(EUTILS + "esearch.fcgi", timeout=30, params={
            "db": "pubmed", "term": term, "retmax": MAX_PAPERS_PER_QUERY,
            "retmode": "json", "sort": "date"})
        ids = r.json()["esearchresult"]["idlist"]
        if not ids:
            continue
        time.sleep(0.5)
        r = requests.get(EUTILS + "efetch.fcgi", timeout=60, params={
            "db": "pubmed", "id": ",".join(ids), "retmode": "xml"})
        root = ET.fromstring(r.content)
        for art in root.iter("PubmedArticle"):
            title_el = art.find(".//ArticleTitle")
            title = "".join(title_el.itertext()) if title_el is not None else ""
            for au in art.iter("Author"):
                aff = " ".join("".join(a.itertext()) for a in au.iter("Affiliation"))
                m = EMAIL_RE.search(aff)
                if not m:
                    continue
                name = f"{au.findtext('ForeName') or ''} {au.findtext('LastName') or ''}"
                if add_lead(leads, m.group(0).rstrip("."), name,
                            EMAIL_RE.sub("", aff).strip(" .;,"), f"PubMed paper: {title}"):
                    added += 1
        save(leads)
        time.sleep(0.5)
    print(f"Done. {added} new leads added.")


def import_csv(path):
    leads = load()
    added = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): (v or "") for k, v in row.items() if k}
            if add_lead(leads, row.get("email", ""), row.get("name", ""),
                        row.get("affiliation", ""), row.get("source", "Imported list")):
                added += 1
    save(leads)
    print(f"Imported. {added} new leads added.")


# ---------- AI helpers ----------
def ask_json(prompt):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY is not set. Follow the setup steps, then reopen your terminal.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"}}
    for _ in range(4):
        r = requests.post(url, json=body, timeout=90, headers={"x-goog-api-key": key})
        if r.status_code in (429, 503):
            print("  Free-tier speed limit reached, waiting 30 seconds...")
            time.sleep(30)
            continue
        if r.status_code in (400, 401, 403, 404):
            sys.exit(f"Gemini error {r.status_code}: {r.text[:300]}\n"
                     "Check your API key and the MODEL name at the top of the file.")
        if r.status_code != 200:
            raise RuntimeError(f"Gemini error {r.status_code}")
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        time.sleep(SECONDS_BETWEEN_CALLS)
        return json.loads(text)
    raise RuntimeError("Still hitting the speed limit. Try again in a few minutes.")


def check_app_config():
    if "Your App Name" in APP_NAME or "One or two lines" in APP_DESCRIPTION:
        sys.exit("Please fill in APP_NAME, APP_DESCRIPTION and IDEAL_CUSTOMER at the top of the file first.")


# ---------- QUALIFY ----------
def qualify(limit=None):
    check_app_config()
    leads = load()
    todo = [l for l in leads if l["status"] == "New"]
    if limit:
        todo = todo[:limit]
    print(f"Scoring {len(todo)} leads...")
    for l in todo:
        prompt = f"""You are screening sales leads for a web app.
APP: {APP_NAME} - {APP_DESCRIPTION}
IDEAL CUSTOMER: {IDEAL_CUSTOMER}

LEAD
Name: {l['name']}
Affiliation: {l['affiliation']}
Found via: {l['source']}

Reply with ONLY a JSON object with three keys:
segment: one of "Researcher/Student", "Company/Lab", "College/Institute", "Other"
score: integer 1 to 5 (5 = ideal fit)
reason: one short sentence
Judge only from the information above. Do not guess beyond it."""
        try:
            res = ask_json(prompt)
            l["segment"] = res["segment"]
            l["score"] = str(res["score"])
            l["reason"] = res["reason"]
            l["status"] = "Qualified" if int(res["score"]) >= MIN_SCORE_TO_DRAFT else "Low fit"
            print(f"  {l['email']}: {l['score']} ({l['segment']})")
        except Exception as e:
            print(f"  Skipped {l['email']}: {e}")
        save(leads)


# ---------- WRITE ----------
def write_drafts_file(leads):
    with open(DRAFTS, "w", encoding="utf-8") as f:
        for l in leads:
            if l["status"] == "Drafted":
                f.write(f"TO: {l['name']} <{l['email']}>\nSUBJECT: {l['subject']}\n\n{l['draft']}\n")
                f.write("\n" + "=" * 60 + "\n\n")


def draft(limit=None):
    check_app_config()
    leads = load()
    todo = [l for l in leads if l["status"] == "Qualified"]
    if limit:
        todo = todo[:limit]
    print(f"Writing {len(todo)} drafts...")
    for l in todo:
        cta = CTA_BY_SEGMENT.get(l["segment"], CTA_BY_SEGMENT["Other"])
        prompt = f"""Write a short, friendly cold email from {SENDER_NAME} ({SENDER_ROLE}).
APP: {APP_NAME} ({APP_URL}) - {APP_DESCRIPTION}
RECIPIENT: {l['name']}, {l['affiliation']}
HOW WE FOUND THEM: {l['source']}
GOAL: {cta}

Rules:
- Under 120 words, plain text, no hype, no emojis.
- Open with one specific, honest reason for writing, based ONLY on the information above. Never invent facts about their work.
- One clear call to action.
- Final line before the sign-off: a polite opt-out such as: If this is not relevant, just reply "no" and I will not email again.
- Sign off with {SENDER_NAME}.
Reply with ONLY a JSON object with two keys: subject and body."""
        try:
            res = ask_json(prompt)
            l["subject"] = res["subject"]
            l["draft"] = res["body"]
            l["status"] = "Drafted"
            print(f"  Drafted for {l['email']}")
        except Exception as e:
            print(f"  Skipped {l['email']}: {e}")
        save(leads)
    write_drafts_file(leads)
    print("Drafts are ready to review (drafts.txt, or the Leads tab in the dashboard).")


# ---------- SEND (Gmail) ----------
def make_message(sender, to, subject, body):
    msg = EmailMessage()
    msg["From"] = f"{SENDER_NAME} <{sender}>"
    msg["To"] = to
    msg["Reply-To"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def send_one(sender, password, msg):
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as server:
        server.login(sender, password.replace(" ", ""))
        server.send_message(msg)


def send_emails(test=False, limit=None):
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not sender or not password:
        sys.exit("GMAIL_ADDRESS and GMAIL_APP_PASSWORD are not set. "
                 "Follow the Gmail setup steps, then reopen your terminal.")
    leads = load()
    approved = [l for l in leads if l["status"] == "Approved" and l["subject"] and l["draft"]]

    try:
        if test:
            sample = approved[0] if approved else None
            subject = "[TEST] " + (sample["subject"] if sample else "Lead agent test")
            body = sample["draft"] if sample else "This is a test email from your lead agent. Sending works."
            send_one(sender, password, make_message(sender, sender, subject, body))
            print(f"Test email sent to {sender}. Check your inbox (and the spam folder).")
            return

        save(leads)  # fails early if the CSV is open in Excel, before anything is sent
        today = dt.date.today()
        sent_today = sum(1 for l in leads if parse_date(l["date_sent"]) == today)
        room = MAX_EMAILS_PER_DAY - sent_today
        if room <= 0:
            print(f"Daily cap reached ({MAX_EMAILS_PER_DAY}). Try again tomorrow.")
            return
        batch = approved[:min(room, limit)] if limit else approved[:room]
        if not batch:
            print("No leads are marked Approved. Open leads_tracker.csv, review the drafts, "
                  "and change status from Drafted to Approved.")
            return
        print(f"Sending {len(batch)} emails (cap {MAX_EMAILS_PER_DAY}/day, already sent today: {sent_today}).")
        print("Keep this window open. Press Ctrl+C any time to stop safely.")
        for i, l in enumerate(batch):
            if not EMAIL_RE.fullmatch(l["email"]):
                print(f"  Skipped {l['email']}: not a valid address")
                continue
            try:
                send_one(sender, password, make_message(sender, l["email"], l["subject"], l["draft"]))
            except smtplib.SMTPAuthenticationError:
                raise
            except Exception as e:
                print(f"  Failed {l['email']}: {e}")
                if "5.4.5" in str(e):
                    print("Gmail daily limit reached. Stop and try again tomorrow.")
                    break
                continue
            l["status"] = "Sent"
            l["date_sent"] = today.isoformat()
            save(leads)
            print(f"  Sent {i + 1}/{len(batch)}: {l['email']}")
            if i < len(batch) - 1:
                time.sleep(random.uniform(*SEND_DELAY_SECONDS))
        print("Done.")
    except smtplib.SMTPAuthenticationError:
        sys.exit("Gmail refused the login. Check GMAIL_ADDRESS and GMAIL_APP_PASSWORD "
                 "(it must be a 16-character App Password, not your normal password).")
    except KeyboardInterrupt:
        print("\nStopped. Everything already sent is saved in the tracker.")


# ---------- TRACK ----------
def report():
    leads = load()
    counts = {}
    for l in leads:
        counts[l["status"]] = counts.get(l["status"], 0) + 1
    print("STATUS COUNTS")
    for status, n in sorted(counts.items()):
        print(f"  {status}: {n}")
    today = dt.date.today()
    print(f"\nFOLLOW-UP DUE (sent {FOLLOWUP_AFTER_DAYS}+ days ago, no reply)")
    found = False
    for l in leads:
        if l["status"] == "Sent":
            d = parse_date(l["date_sent"])
            if d and (today - d).days >= FOLLOWUP_AFTER_DAYS:
                print(f"  {l['name']} <{l['email']}> sent {l['date_sent']}")
                found = True
    if not found:
        print("  None")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "find":
        find_pubmed()
    elif cmd == "import" and len(sys.argv) > 2:
        import_csv(sys.argv[2])
    elif cmd == "qualify":
        qualify()
    elif cmd == "draft":
        draft()
    elif cmd == "send":
        send_emails(test=(len(sys.argv) > 2 and sys.argv[2] == "test"))
    elif cmd == "report":
        report()
    elif cmd == "all":
        find_pubmed()
        qualify()
        draft()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
