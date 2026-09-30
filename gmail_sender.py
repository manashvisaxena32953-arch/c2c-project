"""Gmail SMTP outreach: JD keyword hook, personalization, attachment, duplicate protection,
anti-throttling, email_sent_history.csv logging."""
import csv
import logging
import os
import re
import smtplib
import time
import unicodedata
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid

import client_config as cfg
import linkedin_scraper as scraper
from resume_customizer import customize_resume

log = logging.getLogger(__name__)

HISTORY_FIELDS = [
    "recruiter_name", "recruiter_email", "role", "post_url", "candidate", "email_sent_status",
    "timestamp", "subject", "message_id", "followup_count", "last_followup_at", "replied",
]

# 60+ technology keywords scanned against the job description to build the outreach hook.
JD_KEYWORDS = [
    "Java", "Spring Boot", "Spring", "Microservices", "Hibernate", "J2EE", "Python", "Django",
    "Flask", "FastAPI", "JavaScript", "TypeScript", "React", "ReactJS", "Angular", "Vue", "Node.js",
    "NodeJS", "Express", "Next.js", "Redux", "GraphQL", "REST APIs", "REST", "SOAP", "C#", ".NET",
    ".NET Core", "ASP.NET", "PHP", "Laravel", "Ruby", "Ruby on Rails", "Go", "Golang", "Scala",
    "Kotlin", "Swift", "AWS", "Azure", "GCP", "Google Cloud", "Kubernetes", "Docker", "Terraform",
    "Jenkins", "CI/CD", "Ansible", "DevOps", "SQL", "MySQL", "PostgreSQL", "MongoDB", "Oracle",
    "SQL Server", "Redis", "Cassandra", "DynamoDB", "Kafka", "RabbitMQ", "ETL", "Spark", "Hadoop",
    "Snowflake", "Databricks", "Tableau", "Power BI", "Machine Learning", "TensorFlow", "PyTorch",
    "Data Science", "Selenium", "JUnit", "TestNG", "Cypress", "Playwright", "Salesforce", "SAP",
    "Workday", "ServiceNow", "Git", "GitHub", "GitLab", "Linux", "Agile", "Scrum", "Microservices",
    "gRPC", "Elasticsearch", "HTML", "CSS", "Sass", "Tailwind", "Android", "iOS", "React Native",
    "Flutter",
]
JD_KEYWORDS_RE = [(kw, re.compile(r"\b" + re.escape(kw) + r"\b", re.I)) for kw in JD_KEYWORDS]


# ================================================================== email_sent_history.csv
def _ensure_history_file():
    if not cfg.HISTORY_CSV.exists():
        _write_history([])


def read_history():
    _ensure_history_file()
    with open(cfg.HISTORY_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_history(rows):
    tmp = str(cfg.HISTORY_CSV) + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, cfg.HISTORY_CSV)


def append_history(row: dict):
    rows = read_history()
    rows.append({**{k: "" for k in HISTORY_FIELDS}, **row})
    _write_history(rows)


def update_history_row(recruiter_email: str, candidate: str, **updates):
    rows = read_history()
    changed = False
    for r in rows:
        if r["recruiter_email"].lower() == recruiter_email.lower() and r["candidate"] == candidate:
            r.update({k: str(v) for k, v in updates.items()})
            changed = True
    if changed:
        _write_history(rows)
    return changed


def already_contacted(recruiter_email: str, candidate: str) -> bool:
    """Duplicate protection - checked before every dispatch."""
    key = recruiter_email.strip().lower()
    return any(r["recruiter_email"].strip().lower() == key and r["candidate"] == candidate
               and r["email_sent_status"] == "sent" for r in read_history())


def pending_recruiters(candidate: str):
    """recruiters.csv rows for this candidate's roles, not yet contacted, not mismatched."""
    client = cfg.get_client(candidate)
    roles = set(client["search_roles"])
    rows = [r for r in scraper.read_recruiters()
            if r["role"] in roles and r["email_sent_status"] not in ("mismatched_role",)]
    return [r for r in rows if not already_contacted(r["recruiter_email"], client["name"])]


# ================================================================== hook / personalization
def extract_keywords(job_description: str, candidate_skills=None, top_n: int = 3):
    hits = [kw for kw, rx in JD_KEYWORDS_RE if rx.search(job_description)]
    if candidate_skills:
        skill_set = {s.lower() for s in candidate_skills}
        ranked = [k for k in hits if k.lower() in skill_set] + [k for k in hits if k.lower() not in skill_set]
    else:
        ranked = hits
    seen, out = set(), []
    for k in ranked:
        if k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
        if len(out) >= top_n:
            break
    return out


def build_hook(keywords):
    if not keywords:
        return "My background aligns closely with what you're looking for in this role."
    if len(keywords) == 1:
        joined = keywords[0]
    elif len(keywords) == 2:
        joined = f"{keywords[0]} and {keywords[1]}"
    else:
        joined = f"{', '.join(keywords[:-1])}, and {keywords[-1]}"
    return f"My hands-on experience with {joined} maps directly to what you're looking for."


def build_email(client: dict, recruiter: dict, role: str, hook: str) -> tuple:
    subject = f"{client['name']} — {role} (C2C, {client.get('work_auth', '')})".strip()
    name = recruiter.get("recruiter_name") or "there"
    first_name = name.split()[0] if name and name != "Recruiter" else "there"
    body = f"""Hi {first_name},

I came across your post for the {role} role and wanted to reach out directly. {hook}

A quick snapshot:
- Experience: {client.get('experience', '')}
- Work authorization: {client.get('work_auth', '')}
- Location / relocation: {client.get('location', '')} (relocation: {client.get('relocation', '')})
- Availability: {client.get('availability', '')}

I've attached an updated resume tailored to this role. Happy to jump on a quick call whenever convenient
— let me know a good time, or feel free to reach me directly at {client.get('phone', '')}.

Best regards,
{client['name']}
{client.get('email', '')}
{client.get('linkedin', '')}
"""
    return subject, body


def _cc_list(client: dict, test_mode: bool):
    """Live mode only: CC the client candidate and the team lead (deduplicated, no blanks)."""
    if test_mode:
        return []
    out = []
    for addr in (client.get("email", ""), cfg.LEAD_EMAIL):
        addr = (addr or "").strip()
        if addr and addr.lower() not in {a.lower() for a in out}:
            out.append(addr)
    return out


def _sanitize_filename(name: str) -> str:
    norm = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", norm) or "resume.pdf"


# ================================================================== SMTP
def _connect_smtp():
    email_addr = os.getenv("GMAIL_EMAIL")
    app_pw = os.getenv("GMAIL_APP_PASSWORD")
    if not email_addr or not app_pw:
        raise RuntimeError("GMAIL_EMAIL / GMAIL_APP_PASSWORD not set in .env")
    try:
        s = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(email_addr, app_pw)
        return s, email_addr
    except Exception as e:
        log.warning("STARTTLS:587 failed (%s), falling back to SSL:465", e)
        s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
        s.login(email_addr, app_pw)
        return s, email_addr


def send_outreach(client: dict, recruiter: dict, role: str, resume_pdf, test_mode: bool = True,
                   test_recipient: str = None):
    """Build, personalize and send one outreach email. Returns (status, message_id)."""
    keywords = extract_keywords(recruiter["job_description"], client.get("skills"))
    hook = build_hook(keywords)
    subject, body = build_email(client, recruiter, role, hook)

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="gmail.com")
    to_addr = test_recipient or os.getenv("GMAIL_EMAIL") if test_mode else recruiter["recruiter_email"]
    msg["To"] = to_addr
    cc_list = _cc_list(client, test_mode)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg.attach(MIMEText(body, "plain"))

    fname = _sanitize_filename(f"{client['name'].replace(' ', '')}_Resume_{int(time.time())}.pdf")
    with open(resume_pdf, "rb") as f:
        part = MIMEApplication(f.read(), _subtype="pdf")
    part.add_header("Content-Disposition", "attachment", filename=fname)
    msg.attach(part)

    bcc = [] if test_mode else ([cfg.ADMIN_EMAIL] if cfg.ADMIN_EMAIL else [])
    recipients = [to_addr] + cc_list + bcc

    try:
        server, from_addr = _connect_smtp()
        msg["From"] = from_addr
        server.sendmail(from_addr, recipients, msg.as_string())
        server.quit()
        status = "sent"
    except Exception as e:
        log.error("Send failed to %s: %s", recruiter["recruiter_email"], e)
        status = "failed"

    if test_mode:
        # Test sends go to the operator's own inbox. Recording them would mark the real recruiter
        # as "contacted" and permanently block the live outreach, so nothing is persisted.
        return status, msg["Message-ID"]

    append_history({
        "recruiter_name": recruiter.get("recruiter_name", ""),
        "recruiter_email": recruiter["recruiter_email"],
        "role": role,
        "post_url": recruiter.get("post_url", ""),
        "candidate": client["name"],
        "email_sent_status": status,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "subject": subject,
        "message_id": msg["Message-ID"],
        "followup_count": 0,
        "last_followup_at": "",
        "replied": 0,
    })
    scraper_rows = scraper.read_recruiters()
    for r in scraper_rows:
        if r["recruiter_email"] == recruiter["recruiter_email"] and r["role"] == role:
            r["email_sent_status"] = status
    scraper.write_recruiters(scraper_rows)
    return status, msg["Message-ID"]


def run_outreach_batch(candidate_key: str, test_mode: bool = True, daily_limit: int = None,
                        test_recipient: str = None):
    client = cfg.get_client(candidate_key)
    targets = pending_recruiters(candidate_key)
    if daily_limit:
        targets = targets[:daily_limit]
    if test_mode:
        targets = targets[: min(len(targets), cfg.TEST_MODE_MAX_EMAILS)]

    results = {"sent": 0, "failed": 0, "skipped_duplicate": 0}
    for rec in targets:
        if already_contacted(rec["recruiter_email"], client["name"]):
            results["skipped_duplicate"] += 1
            continue
        try:
            pdf = customize_resume(client, rec["job_description"], rec["role"])
        except Exception as e:
            log.error("Resume customization failed for %s: %s", rec["recruiter_email"], e)
            results["failed"] += 1
            continue
        status, _ = send_outreach(client, rec, rec["role"], pdf, test_mode=test_mode,
                                   test_recipient=test_recipient)
        results["sent" if status == "sent" else "failed"] += 1
        time.sleep(cfg.EMAIL_DELAY_SECONDS)
    return results
