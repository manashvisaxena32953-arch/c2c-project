#!/usr/bin/env python3
"""Recruitment and Outreach Automation System - interactive CLI orchestrator.

Note: Section 4's automatic CSV header validation/migration is intentionally NOT implemented
(skipped per assessment instructions) - both CSVs are written/read with a fixed schema and will
error out rather than silently migrate if you hand-edit their headers.
"""
import logging
import sys

import client_config as cfg
import gmail_sender as gs
import linkedin_login
import linkedin_scraper as scraper
from followup_sender import run_followups
from imap_reply_detector import check_replies

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

MENU = """
==================== C2C Recruitment & Outreach Automation ====================
 1. Validate configuration
 2. Setup / Test LinkedIn login session
 3. Select candidate & scrape target roles
 4. Inspect recruiters.csv / pending records
 5. Run outreach (Test mode)
 6. Run outreach (LIVE mode - requires SEND confirmation)
 7. Check replies (IMAP)
 8. Run full pipeline for ALL candidates (Automated Batch Mode)
 9. Process follow-ups
10. Generate analytics report
 0. Exit
=================================================================================
"""


def _choose_candidate() -> str:
    keys = cfg.list_clients()
    for i, k in enumerate(keys, 1):
        print(f"  {i}. {k} ({cfg.CLIENTS[k]['name']})")
    idx = input("Select candidate #: ").strip()
    try:
        return keys[int(idx) - 1]
    except (ValueError, IndexError):
        print("Invalid selection.")
        return None


def validate_all():
    ok = True
    for key in cfg.list_clients():
        problems = cfg.validate_client(key)
        if problems:
            ok = False
            print(f"[FAIL] {key}: {'; '.join(problems)}")
        else:
            print(f"[OK]   {key}")
    if not cfg.SESSION_FILE.exists():
        print("[WARN] session.json missing - run option 2 before scraping.")
    for var in ("GMAIL_EMAIL", "GMAIL_APP_PASSWORD"):
        import os
        if not os.getenv(var):
            print(f"[WARN] {var} not set in .env")
    return ok


def setup_login():
    if linkedin_login.session_is_valid():
        print("Existing session.json is valid.")
        if input("Re-login anyway? (y/N): ").strip().lower() != "y":
            return
    ok = linkedin_login.login_and_save_session()
    print("Login successful." if ok else "Login failed / timed out.")


def do_scrape():
    key = _choose_candidate()
    if not key:
        return
    client = cfg.get_client(key)
    problems = cfg.validate_client(key)
    if problems:
        print("Cannot scrape - fix these first:", "; ".join(problems))
        return
    roles = client["search_roles"]
    print(f"Roles to scrape: {roles}")
    if not linkedin_login.session_is_valid():
        print("No valid LinkedIn session - starting login...")
        if not linkedin_login.ensure_session():
            print("Aborting - login failed.")
            return
    with scraper.LinkedInSession(headless=True) as session:
        totals = scraper.scrape_candidate(session, key, roles)
    print("Scrape complete:", totals)


def inspect_records():
    rows = scraper.read_recruiters()
    if not rows:
        print("recruiters.csv is empty.")
        return
    counts = {}
    for r in rows:
        counts[r["email_sent_status"]] = counts.get(r["email_sent_status"], 0) + 1
    print(f"Total records: {len(rows)}  |  by status: {counts}")
    show = input("Show pending records for a candidate? (candidate key or blank to skip): ").strip()
    if show:
        try:
            pend = gs.pending_recruiters(show)
        except KeyError as e:
            print(e)
            return
        print(f"{len(pend)} pending record(s) for '{show}':")
        for r in pend[:20]:
            print(f"  - {r['recruiter_name']} <{r['recruiter_email']}> | {r['role']} | {r['post_url']}")


def run_outreach(live: bool):
    key = _choose_candidate()
    if not key:
        return
    client = cfg.get_client(key)
    pend = gs.pending_recruiters(key)
    print(f"{len(pend)} pending recruiter(s) for {client['name']}.")
    if not pend:
        return
    daily_limit = None
    if live:
        raw = input("Global daily sending limit (blank = no limit): ").strip()
        daily_limit = int(raw) if raw.isdigit() else None
        print(f"About to send LIVE outreach to up to {daily_limit or len(pend)} recruiter(s).")
        confirm = input("Type SEND to continue: ").strip()
        if confirm != "SEND":
            print("Aborted - confirmation not received.")
            return
    else:
        print(f"TEST MODE - emails will be sent to your own inbox only (max {cfg.TEST_MODE_MAX_EMAILS}).")
    results = gs.run_outreach_batch(key, test_mode=not live, daily_limit=daily_limit)
    print("Outreach results:", results)


def run_batch_all():
    print("Automated Batch Mode - processes ALL configured candidates.")
    mode = input("Test or Live? [test/live]: ").strip().lower()
    live = mode == "live"
    daily_limit = None
    raw = input("Global daily sending limit (blank = no limit): ").strip()
    daily_limit = int(raw) if raw.isdigit() else None
    if live:
        print(f"About to run LIVE batch outreach for: {cfg.list_clients()} (limit={daily_limit or 'none'})")
        if input("Type SEND to continue: ").strip() != "SEND":
            print("Aborted.")
            return
    if not linkedin_login.ensure_session():
        print("Aborting - LinkedIn login failed.")
        return

    remaining = daily_limit
    with scraper.LinkedInSession(headless=True) as session:
        for key in cfg.list_clients():
            problems = cfg.validate_client(key)
            if problems:
                print(f"[skip] {key}: {'; '.join(problems)}")
                continue
            print(f"\n--- Candidate: {key} ---")
            try:
                totals = scraper.scrape_candidate(session, key)
                print("  scraped:", totals)
            except RuntimeError as e:
                print(f"  scrape error: {e} - stopping batch.")
                break
            limit_for_candidate = remaining if remaining is not None else None
            results = gs.run_outreach_batch(key, test_mode=not live, daily_limit=limit_for_candidate)
            print("  outreach:", results)
            if remaining is not None:
                remaining = max(0, remaining - results["sent"])
                if remaining == 0:
                    print("Global daily limit reached - stopping batch.")
                    break
    print("Batch run complete.")


def do_check_replies():
    try:
        print(check_replies())
    except RuntimeError as e:
        print(f"Error: {e}")


def do_followups():
    key = _choose_candidate()
    if not key:
        return
    mode = input("Test or Live? [test/live]: ").strip().lower()
    live = mode == "live"
    if live and input("Type SEND to continue: ").strip() != "SEND":
        print("Aborted.")
        return
    print(run_followups(key, test_mode=not live))


def do_analytics():
    from analytics import generate_report
    path = generate_report()
    print(f"Report written to {path}")


ACTIONS = {
    "1": validate_all,
    "2": setup_login,
    "3": do_scrape,
    "4": inspect_records,
    "5": lambda: run_outreach(live=False),
    "6": lambda: run_outreach(live=True),
    "7": do_check_replies,
    "8": run_batch_all,
    "9": do_followups,
    "10": do_analytics,
}


def main():
    while True:
        print(MENU)
        choice = input("Select an option: ").strip()
        if choice == "0":
            print("Goodbye.")
            sys.exit(0)
        action = ACTIONS.get(choice)
        if not action:
            print("Invalid option.")
            continue
        try:
            action()
        except Exception as e:
            log.error("Action failed: %s", e, exc_info=True)


if __name__ == "__main__":
    main()
