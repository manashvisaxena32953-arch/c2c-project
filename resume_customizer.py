"""AI resume customization engine.

PDF -> DOCX structural extraction (pdf2docx) -> AI rewrite (Gemini -> OpenRouter -> Groq) ->
style-preserving DOCX rewrite -> PDF (docx2pdf/Word COM, guarded by lock_util).

Client-specific layouts that don't parse cleanly with the default extractor/writer below should get
a dedicated module (see safi_customizer.py / tanuja_customizer.py) hooked in via CUSTOM_DISPATCH.
"""
import importlib
import json
import logging
import os
import platform
import re
import subprocess
import time
from pathlib import Path

import requests
from docx import Document

import client_config as cfg
from lock_util import word_lock

log = logging.getLogger(__name__)

SUMMARY_HEADINGS = {"professional summary", "summary", "profile", "career summary", "professional profile"}
SKILLS_HEADINGS = {"technical skills", "skills", "core competencies", "technologies", "key skills"}
EXPERIENCE_HEADINGS = {
    "professional experience", "work experience", "experience", "projects", "project experience",
    "relevant experience", "employment history",
}
SECTION_DIVIDER_HEADINGS = SUMMARY_HEADINGS | SKILLS_HEADINGS | EXPERIENCE_HEADINGS | {
    "education", "certifications", "certification", "awards", "publications", "references",
    "additional information", "languages", "interests",
}


# ================================================================== dispatcher for client-specific customizers
CUSTOM_DISPATCH = {
    "safi_customizer": "safi_customizer",
    "tanuja_customizer": "tanuja_customizer",
}


def get_customizer_module(client: dict):
    """Return an imported client-specific module if configured and importable, else None."""
    mod_name = client.get("customizer")
    if not mod_name:
        return None
    try:
        return importlib.import_module(CUSTOM_DISPATCH.get(mod_name, mod_name))
    except ImportError:
        log.warning("Custom customizer '%s' not found, falling back to default parser.", mod_name)
        return None


# ================================================================== PDF -> DOCX
def pdf_to_docx(pdf_path: Path) -> Path:
    from pdf2docx import Converter  # heavy import, local to keep CLI startup fast

    out = cfg.SCRATCH_DIR / (pdf_path.stem + "_converted.docx")
    cv = Converter(str(pdf_path))
    try:
        cv.convert(str(out))
    finally:
        cv.close()
    return out


def _heading_key(text: str) -> str:
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


def _is_heading(paragraph) -> bool:
    style = (paragraph.style.name or "").lower()
    txt = paragraph.text.strip()
    if not txt or len(txt) > 60:
        return False
    if "heading" in style or "title" in style:
        return True
    if txt.isupper() and len(txt.split()) <= 6:
        return True
    if _heading_key(txt) in SECTION_DIVIDER_HEADINGS:
        return True
    return False


# ================================================================== structure extraction
def extract_structure(docx_path: Path) -> dict:
    """Extract summary / experience paragraphs / skills from a resume DOCX."""
    doc = Document(str(docx_path))
    summary_paras, experience_paras, skills = [], [], []
    section = None  # "summary" | "experience" | "skills" | "other"

    def classify(key):
        if key in SUMMARY_HEADINGS:
            return "summary"
        if key in EXPERIENCE_HEADINGS:
            return "experience"
        if key in SKILLS_HEADINGS:
            return "skills"
        if key in SECTION_DIVIDER_HEADINGS:
            return "other"
        return None

    for p in doc.paragraphs:
        text = p.text.strip()
        if _is_heading(p):
            key = _heading_key(text)
            new_section = classify(key)
            section = new_section if new_section is not None else "other"
            continue
        if not text:
            continue
        if section == "summary":
            summary_paras.append(text)
        elif section == "experience":
            experience_paras.append(text)
        elif section == "skills":
            skills.extend(_split_skill_line(text))

    for table in doc.tables:
        header_ctx = ""
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            joined = " | ".join(cells)
            if _heading_key(joined) in SKILLS_HEADINGS:
                header_ctx = "skills"
                continue
            for cell in cells:
                skills.extend(_split_skill_line(cell))

    # de-dup skills, keep order
    seen, uniq_skills = set(), []
    for s in skills:
        k = s.lower()
        if k and k not in seen:
            seen.add(k)
            uniq_skills.append(s)

    return {"summary": " ".join(summary_paras), "paragraphs": experience_paras, "skills": uniq_skills}


def _split_skill_line(text: str):
    text = re.sub(r"^[•\-*\u2022]\s*", "", text)
    if ":" in text and len(text.split(":", 1)[0]) < 30:
        text = text.split(":", 1)[1]
    parts = re.split(r"[,;/|]|\s{2,}", text)
    return [p.strip() for p in parts if p.strip() and len(p.strip()) < 40]


# ================================================================== AI rewrite: Gemini -> OpenRouter -> Groq
PROMPT_TEMPLATE = """You are tailoring a candidate's resume to a specific job description.

RULES:
- Preserve the candidate's real work history. Never invent employers, job titles, dates, projects, or education.
- Naturally weave in relevant technologies/keywords from the job description where the candidate's actual
  experience supports it. Do not claim skills/tools with zero basis in the original resume.
- Keep a professional tone and roughly the same length as the original text for each field.
- Respond with ONLY a single JSON object, no markdown fences, no commentary, matching exactly this schema:
{{"rewritten_summary": "string", "rewritten_paragraphs": ["string", ...], "rewritten_skills": ["string", ...]}}

JOB DESCRIPTION:
{jd}

CANDIDATE RESUME STRUCTURE (JSON):
{structure}

Return the rewritten JSON now.
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("No JSON object found in AI response")
    return json.loads(m.group(0))


def _call_gemini(prompt: str) -> dict:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"  # key goes in a header, never in the URL (URLs end up in logs)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "response_mime_type": "application/json"},
    }
    for attempt in range(3):  # Gemini intermittently answers 429/503 under load - retry with backoff
        resp = requests.post(url, headers={"x-goog-api-key": key}, json=payload, timeout=60)
        if resp.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
            break
        time.sleep(2 * (attempt + 1))
    resp.raise_for_status()
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return _extract_json(text)


def _call_openrouter(prompt: str) -> dict:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    model = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.4},
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    return _extract_json(text)


def _call_groq(prompt: str) -> dict:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY not set")
    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.4},
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    return _extract_json(text)


def ai_rewrite(job_description: str, structure: dict) -> dict:
    prompt = PROMPT_TEMPLATE.format(jd=job_description[:4000], structure=json.dumps(structure)[:6000])
    last_err = None
    for name, fn in (("gemini", _call_gemini), ("openrouter", _call_openrouter), ("groq", _call_groq)):
        try:
            result = fn(prompt)
            if not isinstance(result.get("rewritten_paragraphs"), list):
                raise ValueError("malformed AI response")
            log.info("AI rewrite succeeded via %s", name)
            return result
        except Exception as e:
            last_err = e
            log.warning("%s rewrite failed: %s", name, e)
    log.error("All AI providers failed (%s) - keeping original resume text.", last_err)
    return {
        "rewritten_summary": structure.get("summary", ""),
        "rewritten_paragraphs": structure.get("paragraphs", []),
        "rewritten_skills": structure.get("skills", []),
    }


# ================================================================== style-preserving DOCX rewrite
def _set_paragraph_text_preserve_style(paragraph, new_text: str):
    """Overwrite text across a paragraph's runs while preserving run 0's formatting."""
    runs = paragraph.runs
    if not runs:
        paragraph.add_run(new_text)
        return
    runs[0].text = new_text
    for r in runs[1:]:
        r.text = ""


def rewrite_docx(src_docx: Path, structure: dict, rewritten: dict, out_path: Path):
    doc = Document(str(src_docx))
    section = None

    def classify(key):
        if key in SUMMARY_HEADINGS:
            return "summary"
        if key in EXPERIENCE_HEADINGS:
            return "experience"
        if key in SKILLS_HEADINGS:
            return "skills"
        if key in SECTION_DIVIDER_HEADINGS:
            return "other"
        return None

    summary_text = rewritten.get("rewritten_summary", "")
    exp_paras = rewritten.get("rewritten_paragraphs", []) or structure.get("paragraphs", [])
    new_skills = rewritten.get("rewritten_skills", []) or structure.get("skills", [])

    summary_written = False
    exp_idx = 0
    skills_written = False

    for p in doc.paragraphs:
        text = p.text.strip()
        if _is_heading(p):
            key = _heading_key(text)
            new_section = classify(key)
            section = new_section if new_section is not None else "other"
            continue
        if not text:
            continue
        if section == "summary":
            if not summary_written and summary_text:
                _set_paragraph_text_preserve_style(p, summary_text)
                summary_written = True
            elif summary_written:
                _set_paragraph_text_preserve_style(p, "")
        elif section == "experience":
            if exp_idx < len(exp_paras):
                _set_paragraph_text_preserve_style(p, exp_paras[exp_idx])
                exp_idx += 1
            # extra original paragraphs beyond rewritten list are left untouched
        elif section == "skills":
            if not skills_written and new_skills:
                _set_paragraph_text_preserve_style(p, ", ".join(new_skills))
                skills_written = True
            elif skills_written:
                _set_paragraph_text_preserve_style(p, "")

    if section != "skills":
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell.text.strip() and not skills_written and new_skills:
                        for cp in cell.paragraphs:
                            if cp.text.strip():
                                _set_paragraph_text_preserve_style(cp, ", ".join(new_skills))
                                skills_written = True
                                break
                        break

    doc.save(str(out_path))


# ================================================================== DOCX -> PDF
def docx_to_pdf(docx_path: Path, pdf_path: Path):
    with word_lock():
        if platform.system() == "Windows":
            _kill_hung_word()
            from docx2pdf import convert
            convert(str(docx_path), str(pdf_path))
        else:
            # Non-Windows dev/test fallback (no Word COM available)
            subprocess.run(
                ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(pdf_path.parent), str(docx_path)],
                check=True, timeout=120,
            )
            produced = pdf_path.parent / (docx_path.stem + ".pdf")
            if produced != pdf_path and produced.exists():
                produced.replace(pdf_path)


def _kill_hung_word():
    try:
        subprocess.run(["taskkill", "/f", "/im", "winword.exe"], capture_output=True, timeout=15)
        time.sleep(1)
    except Exception:
        pass


# ================================================================== public entry point
def customize_resume(client: dict, job_description: str, role: str) -> Path:
    """Full pipeline for one recruiter/job -> returns path to the customized PDF."""
    custom_mod = get_customizer_module(client)
    if custom_mod and hasattr(custom_mod, "customize_resume"):
        return custom_mod.customize_resume(client, job_description, role)

    resume_path = cfg.resolve_resume(client["key"], role)
    if not resume_path:
        raise FileNotFoundError(f"No resume file available for candidate '{client['key']}'")

    src_docx = pdf_to_docx(resume_path) if resume_path.suffix.lower() == ".pdf" else resume_path
    structure = extract_structure(src_docx)
    rewritten = ai_rewrite(job_description, structure)

    ts = int(time.time())
    safe_name = re.sub(r"[^A-Za-z0-9_]+", "", client["name"].replace(" ", ""))
    out_docx = cfg.SCRATCH_DIR / f"{safe_name}_Resume_{ts}.docx"
    rewrite_docx(src_docx, structure, rewritten, out_docx)

    out_pdf = cfg.SCRATCH_DIR / f"{safe_name}_Resume_{ts}.pdf"
    docx_to_pdf(out_docx, out_pdf)
    return out_pdf
