"""IMAP reply detection - scans Gmail inbox and marks matching recruiters replied=1."""
import email as email_lib
import imaplib
import logging
import os
import re
from datetime import datetime, timedelta

import client_config as cfg
import gmail_sender as gs

log = logging.getLogger(__name__)

BATCH_SIZE = 100
SENDER_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


def _search_since_date() -> str:
    """Oldest pending (sent, unreplied) email's date, else IMAP_FALLBACK_DAYS ago."""
    rows = gs.read_history()
    pending = [r for r in rows if r["email_sent_status"] == "sent" and int(r.get("replied") or 0) == 0]
    dates = []
    for r in pending:
        try:
            dates.append(datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
    since = min(dates) if dates else datetime.now() - timedelta(days=cfg.IMAP_FALLBACK_DAYS)
    return since.strftime("%d-%b-%Y")


def _clean_sender(raw_from: str) -> str:
    m = SENDER_RE.search(raw_from or "")
    return m.group(0).lower() if m else ""


def check_replies() -> dict:
    email_addr = os.getenv("GMAIL_EMAIL")
    app_pw = os.getenv("GMAIL_APP_PASSWORD")
    if not email_addr or not app_pw:
        raise RuntimeError("GMAIL_EMAIL / GMAIL_APP_PASSWORD not set in .env")

    pending_rows = [r for r in gs.read_history()
                    if r["email_sent_status"] == "sent" and int(r.get("replied") or 0) == 0]
    if not pending_rows:
        log.info("No pending outreach - nothing to check.")
        return {"checked": 0, "matched": 0}
    pending_by_email = {}
    for r in pending_rows:
        pending_by_email.setdefault(r["recruiter_email"].lower(), []).append(r)

    since = _search_since_date()
    log.info("IMAP search SINCE %s", since)

    matched = 0
    checked = 0
    conn = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        conn.login(email_addr, app_pw)
        conn.select("INBOX", readonly=True)
        status, data = conn.search(None, f'(SINCE "{since}")')
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        ids = data[0].split()
        for i in range(0, len(ids), BATCH_SIZE):
            batch = ids[i:i + BATCH_SIZE]
            id_set = b",".join(batch)
            status, msg_data = conn.fetch(id_set, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
            if status != "OK":
                continue
            for part in msg_data:
                if not isinstance(part, tuple):
                    continue
                checked += 1
                header = email_lib.message_from_bytes(part[1])
                sender = _clean_sender(header.get("From", ""))
                if sender in pending_by_email:
                    for r in pending_by_email[sender]:
                        gs.update_history_row(r["recruiter_email"], r["candidate"], replied=1)
                    matched += 1
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    log.info("IMAP check complete: %d headers scanned, %d recruiters matched as replied.", checked, matched)
    return {"checked": checked, "matched": matched}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(check_replies())
