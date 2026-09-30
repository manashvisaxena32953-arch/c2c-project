"""Threaded follow-up emails for outreach that hasn't received a reply after FOLLOWUP_DAYS."""
import logging
import os
import smtplib
import time
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import client_config as cfg
import gmail_sender as gs
from resume_customizer import customize_resume

log = logging.getLogger(__name__)


def eligible_followups(days: int = None):
    days = days or cfg.FOLLOWUP_DAYS
    now = datetime.now()
    rows = gs.read_history()
    out = []
    for r in rows:
        if r["email_sent_status"] != "sent" or int(r.get("followup_count") or 0) != 0 or int(r.get("replied") or 0) != 0:
            continue
        try:
            sent_at = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if (now - sent_at).days >= days:
            out.append(r)
    return out


def _ai_value_prop(candidate_skills, job_description: str) -> str:
    try:
        structure = {"summary": "", "paragraphs": [], "skills": candidate_skills}
        prompt_jd = job_description[:3000]
        result = None
        for fn in (_try_openrouter_vp, _try_gemini_vp, _try_groq_vp):
            try:
                result = fn(prompt_jd, candidate_skills)
                if result:
                    break
            except Exception as e:
                log.debug("value-prop provider failed: %s", e)
        return result or _fallback_value_prop(candidate_skills, job_description)
    except Exception:
        return _fallback_value_prop(candidate_skills, job_description)


def _vp_prompt(jd, skills):
    return (
        "Write a concise 1-2 sentence follow-up value proposition connecting these candidate skills: "
        f"{', '.join(skills[:8])} to this job description, professional tone, no preamble:\n\n{jd}"
    )


def _try_openrouter_vp(jd, skills):
    import requests
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("no key")
    model = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": [{"role": "user", "content": _vp_prompt(jd, skills)}], "temperature": 0.5},
        timeout=45,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _try_gemini_vp(jd, skills):
    import requests
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("no key")
    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"  # key goes in a header, never in the URL (URLs end up in logs)
    resp = requests.post(url, headers={"x-goog-api-key": key}, json={"contents": [{"parts": [{"text": _vp_prompt(jd, skills)}]}]}, timeout=45)
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


def _try_groq_vp(jd, skills):
    import requests
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("no key")
    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": [{"role": "user", "content": _vp_prompt(jd, skills)}], "temperature": 0.5},
        timeout=45,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _fallback_value_prop(skills, jd):
    top = ", ".join(skills[:3]) if skills else "my technical background"
    return f"Given my experience with {top}, I believe I'd be a strong fit for this role and would welcome the chance to discuss it further."


def send_followup(client: dict, history_row: dict, test_mode: bool = True, test_recipient: str = None):
    recruiter = {
        "recruiter_email": history_row["recruiter_email"],
        "recruiter_name": history_row["recruiter_name"],
        "job_description": "",
        "role": history_row["role"],
        "post_url": history_row["post_url"],
    }
    matching = [r for r in __import__("linkedin_scraper").read_recruiters()
                if r["recruiter_email"] == recruiter["recruiter_email"] and r["role"] == recruiter["role"]]
    jd = matching[0]["job_description"] if matching else ""

    value_prop = _ai_value_prop(client.get("skills", []), jd)
    subject = "Re: " + history_row["subject"]
    first_name = (history_row["recruiter_name"] or "there").split()[0]
    body = f"""Hi {first_name},

Just following up on my earlier note about the {history_row['role']} opportunity. {value_prop}

Re-attaching my resume for convenience — happy to connect whenever works for you.

Best regards,
{client['name']}
{client.get('email', '')}
"""
    try:
        pdf = customize_resume(client, jd, history_row["role"]) if jd else None
    except Exception as e:
        log.warning("Could not regenerate resume for follow-up, sending without new customization: %s", e)
        pdf = None

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["In-Reply-To"] = history_row["message_id"]
    msg["References"] = history_row["message_id"]
    to_addr = test_recipient or os.getenv("GMAIL_EMAIL") if test_mode else recruiter["recruiter_email"]
    msg["To"] = to_addr
    cc_list = gs._cc_list(client, test_mode)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg.attach(MIMEText(body, "plain"))
    if pdf:
        with open(pdf, "rb") as f:
            part = MIMEApplication(f.read(), _subtype="pdf")
        part.add_header("Content-Disposition", "attachment",
                         filename=gs._sanitize_filename(f"{client['name']}_Resume_Followup.pdf"))
        msg.attach(part)

    bcc = [] if test_mode else ([cfg.ADMIN_EMAIL] if cfg.ADMIN_EMAIL else [])
    recipients = [to_addr] + cc_list + bcc
    try:
        server, from_addr = gs._connect_smtp()
        msg["From"] = from_addr
        server.sendmail(from_addr, recipients, msg.as_string())
        server.quit()
        ok = True
    except Exception as e:
        log.error("Follow-up send failed to %s: %s", recruiter["recruiter_email"], e)
        ok = False

    if ok and not test_mode:  # a test follow-up goes to the operator only; don't consume the real one
        gs.update_history_row(
            history_row["recruiter_email"], client["name"],
            followup_count=int(history_row.get("followup_count") or 0) + 1,
            last_followup_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    return ok


def run_followups(candidate_key: str, test_mode: bool = True, test_recipient: str = None):
    client = cfg.get_client(candidate_key)
    rows = [r for r in eligible_followups() if r["candidate"] == client["name"]]
    sent, failed = 0, 0
    for r in rows:
        ok = send_followup(client, r, test_mode=test_mode, test_recipient=test_recipient)
        sent += ok
        failed += not ok
        time.sleep(cfg.EMAIL_DELAY_SECONDS)
    return {"sent": sent, "failed": failed, "eligible": len(rows)}
