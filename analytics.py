"""Pipeline analytics -> analytics_report.html (dark-themed dashboard)."""
import html
import json
import logging
from collections import defaultdict
from datetime import datetime

import client_config as cfg
import gmail_sender as gs
import linkedin_scraper as scraper

log = logging.getLogger(__name__)


def _scrape_totals():
    totals = {"scraped": 0, "valid": 0, "rejected": 0}
    if cfg.STATS_JSON.exists():
        try:
            data = json.loads(cfg.STATS_JSON.read_text(encoding="utf-8"))
            for run in data.get("runs", []):
                totals["scraped"] += run.get("scraped", 0)
                totals["valid"] += run.get("valid", 0)
                totals["rejected"] += run.get("rejected", 0)
        except Exception:
            pass
    return totals


def compute_stats() -> dict:
    history = gs.read_history()
    recruiters = scraper.read_recruiters()
    scrape_totals = _scrape_totals()

    sent = [r for r in history if r["email_sent_status"] == "sent"]
    failed = [r for r in history if r["email_sent_status"] == "failed"]
    replied = [r for r in history if int(r.get("replied") or 0) == 1]
    followups = sum(int(r.get("followup_count") or 0) for r in history)
    pending = [r for r in recruiters if r["email_sent_status"] == "pending"]

    by_candidate = defaultdict(lambda: {"sent": 0, "failed": 0, "replied": 0})
    by_role = defaultdict(lambda: {"sent": 0, "failed": 0, "replied": 0})
    for r in history:
        c, role = r["candidate"], r["role"]
        if r["email_sent_status"] == "sent":
            by_candidate[c]["sent"] += 1
            by_role[role]["sent"] += 1
        elif r["email_sent_status"] == "failed":
            by_candidate[c]["failed"] += 1
            by_role[role]["failed"] += 1
        if int(r.get("replied") or 0) == 1:
            by_candidate[c]["replied"] += 1
            by_role[role]["replied"] += 1

    reply_rate = (len(replied) / len(sent) * 100) if sent else 0.0

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scraped_posts": scrape_totals["scraped"],
        "valid_recruiters": scrape_totals["valid"] or len([r for r in recruiters if r["email_sent_status"] != "mismatched_role"]),
        "rejected_posts": scrape_totals["rejected"],
        "emails_sent": len(sent),
        "emails_failed": len(failed),
        "followups_sent": followups,
        "replies": len(replied),
        "reply_rate": round(reply_rate, 1),
        "pending_outreach": len(pending),
        "replied_outreach": len(replied),
        "by_candidate": dict(by_candidate),
        "by_role": dict(by_role),
    }


CARD_TPL = """<div class="card"><div class="card-value">{value}</div><div class="card-label">{label}</div></div>"""
CARD_TPL_ACCENT = """<div class="card reply-rate"><div class="card-value">{value}</div><div class="card-label">{label}</div></div>"""

ROW_TPL = """<tr><td>{name}</td><td>{sent}</td><td>{failed}</td><td>{replied}</td></tr>"""

HTML_TPL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Outreach Analytics</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 40px; background: #0b0f19; color: #e6e9f0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  }}
  h1 {{ font-size: 24px; margin: 0 0 4px; }}
  .sub {{ color: #8892a6; margin: 0 0 28px; font-size: 13px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 36px; }}
  .card {{
    background: linear-gradient(160deg, #141b2d, #10152280); border: 1px solid #232a3d;
    border-radius: 12px; padding: 18px 20px;
  }}
  .card-value {{ font-size: 28px; font-weight: 700; color: #7dd3fc; }}
  .card-label {{ font-size: 12px; color: #8892a6; text-transform: uppercase; letter-spacing: .04em; margin-top: 4px; }}
  .reply-rate .card-value {{ color: #86efac; }}
  h2 {{ font-size: 16px; margin: 32px 0 12px; color: #cbd3e6; }}
  table {{ width: 100%; border-collapse: collapse; background: #10152280; border-radius: 10px; overflow: hidden; }}
  th, td {{ text-align: left; padding: 10px 14px; font-size: 13px; border-bottom: 1px solid #1e2536; }}
  th {{ color: #8892a6; font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: .04em; }}
  tr:last-child td {{ border-bottom: none; }}
</style>
</head>
<body>
  <h1>Recruitment Outreach Analytics</h1>
  <p class="sub">Generated {generated_at}</p>

  <div class="grid">
    {card_scraped}
    {card_valid}
    {card_rejected}
    {card_sent}
    {card_failed}
    {card_followups}
    {card_replies}
    {card_reply_rate}
    {card_pending}
  </div>

  <h2>By Candidate</h2>
  <table>
    <tr><th>Candidate</th><th>Sent</th><th>Failed</th><th>Replied</th></tr>
    {candidate_rows}
  </table>

  <h2>By Role</h2>
  <table>
    <tr><th>Role</th><th>Sent</th><th>Failed</th><th>Replied</th></tr>
    {role_rows}
  </table>
</body>
</html>
"""


def generate_report() -> str:
    s = compute_stats()
    candidate_rows = "".join(ROW_TPL.format(name=html.escape(k or "(unknown)"), **v) for k, v in s["by_candidate"].items()) or \
        "<tr><td colspan='4'>No data yet</td></tr>"
    role_rows = "".join(ROW_TPL.format(name=html.escape(k or "(unknown)"), **v) for k, v in s["by_role"].items()) or \
        "<tr><td colspan='4'>No data yet</td></tr>"

    page = HTML_TPL.format(
        generated_at=s["generated_at"],
        card_scraped=CARD_TPL.format(value=s["scraped_posts"], label="Scraped Posts"),
        card_valid=CARD_TPL.format(value=s["valid_recruiters"], label="Valid Recruiters"),
        card_rejected=CARD_TPL.format(value=s["rejected_posts"], label="Rejected Posts"),
        card_sent=CARD_TPL.format(value=s["emails_sent"], label="Emails Sent"),
        card_failed=CARD_TPL.format(value=s["emails_failed"], label="Failed Emails"),
        card_followups=CARD_TPL.format(value=s["followups_sent"], label="Follow-ups Sent"),
        card_replies=CARD_TPL.format(value=s["replies"], label="Replies"),
        card_reply_rate=CARD_TPL_ACCENT.format(value=f'{s["reply_rate"]}%', label="Reply Rate"),
        card_pending=CARD_TPL.format(value=s["pending_outreach"], label="Pending Outreach"),
        candidate_rows=candidate_rows,
        role_rows=role_rows,
    )
    cfg.ANALYTICS_HTML.write_text(page, encoding="utf-8")
    log.info("Analytics report written to %s", cfg.ANALYTICS_HTML)
    return str(cfg.ANALYTICS_HTML)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(generate_report())
