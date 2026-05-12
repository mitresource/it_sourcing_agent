"""
Render Resume PDF Node

Takes formatted and tuned structured JSON resumes from state and
renders them as .pdf files saved locally under:
  agent_resume_results/<job_folder>/pdf/<name>_formatted.pdf
  agent_resume_results/<job_folder>/pdf/<name>_tuned.pdf
"""

import os
from typing import Dict, Any

import boto3
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import black
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_LEFT

from agents.db import bench_candidate_evaluation

S3_BUCKET = "bench-candidates"
S3_REGION = "ap-south-1"


def _upload_pdf_to_s3(local_path: str, job_id: str, candidate_name: str, folder: str) -> str:
    """Upload a rendered PDF to bench-candidates S3. Returns the public URL."""
    safe_name = candidate_name.replace(" ", "_")
    key = f"{folder}/{job_id}/{safe_name}_{folder}.pdf"
    s3 = boto3.client("s3", region_name=S3_REGION)
    with open(local_path, "rb") as f:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=f.read(),
            ContentType="application/pdf",
            ContentDisposition="inline",
        )
    return f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{key}"


# ── helpers ────────────────────────────────────────────────────────────────

def _safe_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in text)


def _output_dir(base: str, structured_jd: dict, job_id: str) -> str:
    job_title = structured_jd.get("job_title", "unknown_job")
    safe_title = _safe_name(job_title)
    run_folder = f"{safe_title}_{job_id[:8]}"
    path = os.path.join(base, run_folder, "pdf")
    os.makedirs(path, exist_ok=True)
    return path


def _styles() -> dict:
    return {
        "name":    ParagraphStyle("name",    fontSize=16,   fontName="Helvetica-Bold",    alignment=TA_CENTER, spaceAfter=2),
        "contact": ParagraphStyle("contact", fontSize=9,    fontName="Helvetica",          alignment=TA_CENTER, spaceAfter=6),
        "section": ParagraphStyle("section", fontSize=10.5, fontName="Helvetica-Bold",    spaceBefore=4, spaceAfter=2),
        "body":    ParagraphStyle("body",    fontSize=9.5,  fontName="Helvetica",          spaceAfter=1, leading=13),
        "bullet":  ParagraphStyle("bullet",  fontSize=9.5,  fontName="Helvetica",          leftIndent=12, spaceAfter=1, leading=13),
        "company": ParagraphStyle("company", fontSize=10,   fontName="Helvetica-Bold",    spaceBefore=6, spaceAfter=1),
        "title":   ParagraphStyle("title",   fontSize=9.5,  fontName="Helvetica-Oblique", spaceAfter=1),
        "env":     ParagraphStyle("env",     fontSize=9,    fontName="Helvetica-Oblique", leftIndent=12, spaceAfter=2),
    }


def _hr(story: list):
    story.append(HRFlowable(width="100%", thickness=0.5, color=black, spaceAfter=2, spaceBefore=8))


# ── core renderer ───────────────────────────────────────────────────────────

def _build_pdf(resume: dict, output_path: str):
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
    )

    s = _styles()
    story = []

    profile = resume.get("candidate_profile_details", {})
    name = profile.get("name", "Candidate")
    email = profile.get("email", "")
    linkedin = profile.get("linkedin_id", "")

    # ── Name ──
    story.append(Paragraph(name, s["name"]))

    # ── Contact line ──
    contact_parts = [p for p in [email, linkedin] if p]
    if contact_parts:
        story.append(Paragraph("  |  ".join(contact_parts), s["contact"]))

    # ── Professional Summary ──
    summary_bullets = resume.get("profile_summary", [])
    if summary_bullets:
        _hr(story)
        story.append(Paragraph("PROFESSIONAL SUMMARY", s["section"]))
        for bullet in summary_bullets:
            story.append(Paragraph(f"• {bullet}", s["bullet"]))

    # ── Skills ──
    skills = resume.get("skills", {})
    if skills:
        _hr(story)
        story.append(Paragraph("SKILLS", s["section"]))
        for category, items in skills.items():
            if not items:
                continue
            items_str = ", ".join(items) if isinstance(items, list) else str(items)
            story.append(Paragraph(f"<b>{category}:</b> {items_str}", s["body"]))

    # ── Education ──
    education = resume.get("education", "")
    if education:
        _hr(story)
        story.append(Paragraph("EDUCATION", s["section"]))
        story.append(Paragraph(education, s["body"]))

    # ── Work Experience ──
    work_exp = resume.get("work_experience", [])
    if work_exp:
        _hr(story)
        story.append(Paragraph("WORK EXPERIENCE", s["section"]))
        for exp in work_exp:
            employer  = exp.get("employer_name", "") or exp.get("client_name", "")
            client    = exp.get("client_name", "") if exp.get("employer_name") else ""
            location  = exp.get("client_location", "")
            title     = exp.get("Candidate_designation", "")
            timeframe = exp.get("time_frame", "")
            desc      = exp.get("project_description", "")
            resps     = exp.get("responsibilities", [])
            env       = exp.get("environment", "")

            company_text = f"{employer}  —  {location}" if location else employer
            story.append(Paragraph(company_text, s["company"]))
            if client:
                story.append(Paragraph(f"<i>Client: {client}</i>", s["title"]))

            if title and title.lower() != "nan":
                title_text = f"<i>{title}</i>"
                if timeframe:
                    title_text += f"  ({timeframe})"
                story.append(Paragraph(title_text, s["title"]))

            if desc:
                story.append(Paragraph(desc, s["body"]))

            for resp in resps:
                story.append(Paragraph(f"• {resp}", s["bullet"]))

            if env:
                story.append(Paragraph(f"<b>Environment:</b> <i>{env}</i>", s["env"]))

    doc.build(story)


# ── node ────────────────────────────────────────────────────────────────────

async def render_resume_docx(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node: Render formatted and tuned structured resumes to .pdf files,
    saved locally and uploaded to S3 with inline content disposition.
    """
    current_job_id = state.get("current_job_id", "")
    structured_jd = state.get("structured_jd", {})

    base_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "agent_resume_results",
    )
    out_dir = _output_dir(base_dir, structured_jd, current_job_id)

    # Lookup: job_id → job data S3 presigned URL
    job_data_url_lookup = {
        r["job_id"]: r["url"]
        for r in state.get("job_data_urls", [])
        if r.get("url")
    }
    job_data_url = job_data_url_lookup.get(current_job_id)

    rendered_paths = []
    formatted_docx_urls = []
    tuned_docx_urls = []

    # ── Formatted resumes ──
    formatted_list = state.get("formatted_resume_data", [])
    current_formatted = [r for r in formatted_list if r.get("job_id") == current_job_id]

    for item in current_formatted:
        name   = item.get("candidate_name", "candidate")
        resume = item.get("formatted_resume", {})
        if not resume:
            continue
        safe = _safe_name(name)
        path = os.path.join(out_dir, f"{safe}_formatted.pdf")
        _build_pdf(resume, path)
        rendered_paths.append(path)
        print(f"   📄 Saved formatted PDF: {path}")

        try:
            url = _upload_pdf_to_s3(path, current_job_id, name, "formatted_resume")
            candidate_id = item.get("candidate_id", "")
            formatted_docx_urls.append({
                "job_id": current_job_id,
                "candidate_id": candidate_id,
                "candidate_name": name,
                "url": url,
            })
            print(f"   ☁️  Uploaded formatted PDF → {url}")
            try:
                update_fields = {"formatted_shortlisted_resume_s3_url": url}
                if job_data_url:
                    update_fields["job_data_s3_url"] = job_data_url
                await bench_candidate_evaluation.update_one(
                    {"job_id": current_job_id, "candidate_id": candidate_id},
                    {"$set": update_fields},
                )
            except Exception as db_err:
                print(f"   WARNING: MongoDB update failed for formatted PDF ({name}): {db_err}")
        except Exception as e:
            print(f"   WARNING: S3 upload failed for formatted PDF ({name}): {e}")

    # ── Tuned resumes ──
    tuned_list = state.get("tuned_candidates", [])
    current_tuned = [r for r in tuned_list if r.get("job_id") == current_job_id]

    for item in current_tuned:
        name   = item.get("candidate_name", "candidate")
        resume = item.get("structured_tuned_resume", {})
        if not resume:
            continue
        safe = _safe_name(name)
        path = os.path.join(out_dir, f"{safe}_tuned.pdf")
        _build_pdf(resume, path)
        rendered_paths.append(path)
        print(f"   📄 Saved tuned PDF:      {path}")

        try:
            url = _upload_pdf_to_s3(path, current_job_id, name, "tuned_resume")
            candidate_id = item.get("candidate_id", "")
            tuned_docx_urls.append({
                "job_id": current_job_id,
                "candidate_id": candidate_id,
                "candidate_name": name,
                "url": url,
            })
            print(f"   ☁️  Uploaded tuned PDF → {url}")
            try:
                update_fields = {"tuned_formatted_shortlisted_resume_s3_url": url}
                if job_data_url:
                    update_fields["job_data_s3_url"] = job_data_url
                await bench_candidate_evaluation.update_one(
                    {"job_id": current_job_id, "candidate_id": candidate_id},
                    {"$set": update_fields},
                )
            except Exception as db_err:
                print(f"   WARNING: MongoDB update failed for tuned PDF ({name}): {db_err}")
        except Exception as e:
            print(f"   WARNING: S3 upload failed for tuned PDF ({name}): {e}")

    if rendered_paths:
        print(f"\n✅ {len(rendered_paths)} PDF file(s) saved to: {out_dir}")
    else:
        print("   No structured resume data to render as PDF.")

    return {
        "rendered_docx_paths": rendered_paths,
        "formatted_docx_urls": formatted_docx_urls,
        "tuned_docx_urls": tuned_docx_urls,
    }
