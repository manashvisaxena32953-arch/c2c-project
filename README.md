# C2C Recruitment & Outreach Automation

Command-line pipeline that finds C2C (Corp-to-Corp) hiring posts on LinkedIn, extracts recruiter
emails, tailors a candidate's resume to each job description with an LLM, and sends outreach emails
through Gmail. It also sends threaded follow-ups, detects replies over IMAP, and produces an
analytics dashboard. Built from `C2C_project_documentation.pdf`.

## Requirements

- **Python 3.10+**
- **Windows with Microsoft Word installed** for the final DOCX -> PDF step (`docx2pdf` drives Word
  through COM). On other systems `resume_customizer.py` falls back to LibreOffice
  (`soffice --headless --convert-to pdf`) if it is installed; this is for local testing only.
- A Gmail account with 2-Step Verification and a **Gmail App Password**
- At least one LLM key (Gemini, Groq or OpenRouter). Without any key the pipeline still runs,
  but the resume is sent **unmodified**.

## Installation

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Configuration (safe setup)

1. Copy the template and fill in your own values:
   ```bash
   copy .env.example .env        # Windows   (Linux/macOS: cp .env.example .env)
   ```
2. In `.env` set `GMAIL_EMAIL`, `GMAIL_APP_PASSWORD` and at least one of `GEMINI_API_KEY`,
   `GROQ_API_KEY`, `OPENROUTER_API_KEY`. `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` are optional and
   only pre-fill the login form; you always finish the login yourself in the browser.
   **Delete or blank every line you do not use** (for example the Groq/OpenRouter keys) - a
   leftover placeholder such as `your_groq_api_key` would be treated as a real key and fail.
3. Edit `client_config.py`: replace the placeholder entries under `CLIENTS` and
   `CANDIDATE_ROLES` with real candidates and put each resume (.docx or .pdf) in `clients/`.
   `clients/safi_resume.docx` is a **fictional sample** so the config check passes; replace it.

Never commit or share `.env` or `session.json` - they contain your credentials and login session.
Both are created locally and are not part of this package.

## Run

```bash
python main.py
```

The program is menu driven (there are no command-line flags):

| Option | What it does |
|---|---|
| 1 | Validate configuration (resume files, Gmail credentials, session) |
| 2 | Open a browser to log in to LinkedIn once and save `session.json` |
| 3 | Pick a candidate and scrape their target roles into `recruiters.csv` |
| 4 | Show record counts and pending recruiters for a candidate |
| 5 | Outreach in **Test mode** - emails go only to your own inbox (max 3); nothing is recorded, so real recruiters stay pending |
| 6 | Outreach in **Live mode** - asks for a daily limit and requires typing `SEND` |
| 7 | Check Gmail (IMAP) for replies and mark them `replied=1` |
| 8 | Automated batch run for all candidates |
| 9 | Send threaded follow-ups to unanswered emails (default 7 days) |
| 10 | Generate `analytics_report.html` |

Recommended first run: 1 -> 2 -> 3 -> 4 -> 5 (check your inbox) -> 6.

## Data files

- `recruiters.csv` - scraped recruiters (`pending`, `sent`, `failed`, `mismatched_role`).
- `email_sent_history.csv` - one row per outreach/follow-up event. Duplicate protection: a recruiter
  is not emailed again by the same candidate, but different candidates may contact the same
  recruiter. Both files are created automatically with the correct headers if missing; the
  packaged copies contain only headers.

Note: the automatic CSV header migration from Section 4 of the documentation was intentionally not
implemented; do not hand-edit the CSV headers.

## Analytics

Option 10 (or `python analytics.py`) writes `analytics_report.html`: scraped posts, valid
recruiters, rejected posts, emails sent, failed emails, follow-ups, replies, reply rate,
pending outreach, and breakdowns by candidate and by role. It only reports what is in the CSV files
and `scrape_stats.json`; open the file in any browser.

## Files

```
main.py                  interactive CLI orchestrator
client_config.py         candidate profiles, roles, filter config (edit this)
linkedin_login.py        headful Playwright login -> session.json
linkedin_scraper.py      search, scroll, filter, extract -> recruiters.csv
resume_customizer.py     PDF/DOCX -> LLM rewrite (Gemini -> OpenRouter -> Groq) -> DOCX -> PDF
safi_customizer.py       per-candidate resume layout hooks
tanuja_customizer.py
gmail_sender.py          SMTP outreach, JD keyword hook, duplicate protection, history logging
followup_sender.py       threaded follow-ups (In-Reply-To / References)
imap_reply_detector.py   IMAP inbox scan -> replied=1
analytics.py             analytics_report.html dark dashboard
lock_util.py             file-based mutex for Word COM calls
clients/                 candidate resumes
scratch/                 temporary DOCX/PDF working files
```

## Known limitations

- The LinkedIn scraper depends on LinkedIn's page structure, which changes often. In testing it
  found recruiter emails reliably but did not always recover the recruiter's name or the post link
  (`post_url` can be empty and the greeting falls back to "Recruiter").
- Gemini and Groq retire model names regularly (Groq's `llama-3.3-70b-versatile` was retired on
  2026-08-16). A 404 from either provider usually means the model name is outdated: set
  `GEMINI_MODEL` / `GROQ_MODEL` in `.env` to a current model. Also make sure each line in `.env` has
  the form `NAME=value` with the name written only once.
- Gmail sending limits apply; start with a small daily limit.
