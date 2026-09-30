"""Dedicated resume customizer for candidate 'safi' — hook point for a non-standard layout.

Currently reuses the default extraction/AI/rewrite pipeline from resume_customizer.py; replace the
body of `customize_resume` with candidate-specific parsing/writing if the default parser mis-reads
this candidate's resume (see docstring in resume_customizer.get_customizer_module).
"""
import re
import time
from pathlib import Path

import client_config as cfg
import resume_customizer as rc


def customize_resume(client: dict, job_description: str, role: str) -> Path:
    resume_path = cfg.resolve_resume(client["key"], role)
    if not resume_path:
        raise FileNotFoundError(f"No resume file available for candidate '{client['key']}'")

    src_docx = rc.pdf_to_docx(resume_path) if resume_path.suffix.lower() == ".pdf" else resume_path
    structure = rc.extract_structure(src_docx)
    rewritten = rc.ai_rewrite(job_description, structure)

    ts = int(time.time())
    safe_name = re.sub(r"[^A-Za-z0-9_]+", "", client["name"].replace(" ", ""))
    out_docx = cfg.SCRATCH_DIR / f"{safe_name}_Resume_{ts}.docx"
    rc.rewrite_docx(src_docx, structure, rewritten, out_docx)

    out_pdf = cfg.SCRATCH_DIR / f"{safe_name}_Resume_{ts}.pdf"
    rc.docx_to_pdf(out_docx, out_pdf)
    return out_pdf
