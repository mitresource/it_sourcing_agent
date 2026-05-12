from typing import Dict, Any
from bson import ObjectId
import os
import tempfile
import sys
sys.path.append("..")

import boto3
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, black
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_LEFT

from agents.db import job_collection_name
from agents.utils.s3_utils import generate_presigned_url
from .state import RecruitmentState

S3_BUCKET = "bench-candidates"
S3_REGION = "ap-south-1"

BLUE = HexColor("#2980b9")
DARK = HexColor("#2c3e50")
GREY = HexColor("#7f8c8d")


def _jd_styles() -> dict:
    return {
        "title":    ParagraphStyle("jd_title",    fontSize=18, fontName="Helvetica-Bold",
                                   alignment=TA_CENTER, spaceAfter=4, textColor=DARK),
        "company":  ParagraphStyle("jd_company",  fontSize=12, fontName="Helvetica-Bold",
                                   alignment=TA_CENTER, spaceAfter=2, textColor=BLUE),
        "meta":     ParagraphStyle("jd_meta",     fontSize=9,  fontName="Helvetica",
                                   alignment=TA_CENTER, spaceAfter=8, textColor=GREY),
        "section":  ParagraphStyle("jd_section",  fontSize=11, fontName="Helvetica-Bold",
                                   spaceBefore=10, spaceAfter=4, textColor=DARK),
        "body":     ParagraphStyle("jd_body",     fontSize=9.5, fontName="Helvetica",
                                   spaceAfter=2, leading=14),
        "bullet":   ParagraphStyle("jd_bullet",   fontSize=9.5, fontName="Helvetica",
                                   leftIndent=14, spaceAfter=2, leading=14),
        "link":     ParagraphStyle("jd_link",     fontSize=9,  fontName="Helvetica-Oblique",
                                   textColor=BLUE, spaceAfter=4),
    }


def _hr(story: list):
    story.append(HRFlowable(width="100%", thickness=0.5, color=BLUE, spaceAfter=4, spaceBefore=6))


def _build_job_pdf(job: dict, output_path: str):
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
    )
    s = _jd_styles()
    story = []

    # ── Header ──
    story.append(Paragraph(job.get("title", "Job Description"), s["title"]))
    if job.get("company"):
        story.append(Paragraph(job["company"], s["company"]))

    meta_parts = [p for p in [
        job.get("location", ""),
        job.get("job_type", ""),
        job.get("experience", ""),
        (f"Salary: {job['salary']}" if job.get("salary") else ""),
        (f"Posted: {job['posted_date']}" if job.get("posted_date") else ""),
    ] if p]
    if meta_parts:
        story.append(Paragraph("  |  ".join(meta_parts), s["meta"]))

    if job.get("job_url"):
        story.append(Paragraph(f"Job URL: {job['job_url']}", s["link"]))

    _hr(story)

    # ── Required Skills ──
    skills = job.get("required_skills", [])
    if skills:
        story.append(Paragraph("REQUIRED SKILLS", s["section"]))
        story.append(Paragraph(", ".join(skills), s["body"]))
        _hr(story)

    # ── Responsibilities ──
    responsibilities = job.get("responsibilities", [])
    if responsibilities:
        story.append(Paragraph("RESPONSIBILITIES", s["section"]))
        for item in responsibilities:
            story.append(Paragraph(f"• {item}", s["bullet"]))
        _hr(story)

    # ── Qualifications ──
    qualifications = job.get("qualifications", [])
    if qualifications:
        story.append(Paragraph("QUALIFICATIONS", s["section"]))
        for item in qualifications:
            story.append(Paragraph(f"• {item}", s["bullet"]))
        _hr(story)

    # ── Full Description ──
    description = job.get("full_description", "")
    if description:
        story.append(Paragraph("FULL JOB DESCRIPTION", s["section"]))
        for line in description.splitlines():
            line = line.strip()
            if not line:
                story.append(Spacer(1, 4))
            elif line.startswith(("•", "-", "*")):
                story.append(Paragraph(f"• {line.lstrip('•-* ').strip()}", s["bullet"]))
            else:
                story.append(Paragraph(line, s["body"]))

    doc.build(story)


def _upload_job_pdf_to_s3(local_path: str, job_id: str) -> str:
    key = f"dice_jobs/{job_id}/job_description.pdf"
    s3 = boto3.client("s3", region_name=S3_REGION)
    with open(local_path, "rb") as f:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=f.read(),
            ContentType="application/pdf",
            ContentDisposition="inline",
        )
    return key


async def prepare_job_data(state: Dict[str, Any]) -> Dict[str, Any]:
    if not state.get("pending_jobs_id"):
        print("No pending jobs found in state.")
        return state

    index = state.get("current_job_index", 0)
    job_id = state["pending_jobs_id"][index]
    print(f'Preparing jobs data for job {index + 1}/{len(state["pending_jobs_id"])} (id: {job_id})')


    # fetch the full job document (convert string back to ObjectId)
    job_doc = await job_collection_name.find_one({"_id": ObjectId(job_id)})

    if not job_doc:
        print(f'No job document found for job_id: {job_id}')
        return state
    
    # Fields from IT_jobs.Scrapper_jobs_agent schema
    required_skills = job_doc.get("skills", []) or []
    responsibilities = job_doc.get("responsibilities", []) or []

    structured_jd = {
        "job_title": job_doc.get("title", ""),
        "company": job_doc.get("company", ""),
        "location": job_doc.get("location", ""),
        "job_type": job_doc.get("job_type", ""),
        "experience": job_doc.get("experience", ""),
        "salary": job_doc.get("salary", ""),
        "required_skills": required_skills,
        "responsibilities": responsibilities,
        "qualifications": job_doc.get("qualifications", []) or [],
        "description": job_doc.get("full_summary", "") or job_doc.get("raw_text", ""),
        "posted_date": job_doc.get("posted_date", ""),
        "apply_link": job_doc.get("apply_link", ""),
        "job_url": job_doc.get("url", ""),
        "original_job": job_doc,
    }

    print(f"✅ Job prepared: {structured_jd.get('job_title')} at {structured_jd.get('company')}")
    print(f"   Required skills: {len(structured_jd['required_skills'])} skills")

    # Render job description as PDF and upload to S3
    job_data_url = None
    try:
        job_payload = {
            "title": structured_jd["job_title"],
            "company": structured_jd["company"],
            "location": structured_jd["location"],
            "job_type": structured_jd["job_type"],
            "experience": structured_jd["experience"],
            "salary": structured_jd["salary"],
            "posted_date": structured_jd["posted_date"],
            "job_url": structured_jd["job_url"],
            "required_skills": structured_jd["required_skills"],
            "responsibilities": structured_jd["responsibilities"],
            "qualifications": structured_jd["qualifications"],
            "full_description": structured_jd["description"],
        }
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
        _build_job_pdf(job_payload, tmp_path)
        s3_key = _upload_job_pdf_to_s3(tmp_path, str(job_id))
        os.unlink(tmp_path)
        job_data_url = generate_presigned_url(s3_key)
        print(f"   Job description PDF uploaded → dice_jobs/{job_id}/job_description.pdf")
    except Exception as e:
        print(f"   WARNING: Could not upload job description PDF to S3: {e}")

    return {
        "current_job_id": str(job_id),
        "current_job": job_doc,
        "raw_jd": job_doc.get("full_summary") or job_doc.get("raw_text"),
        "structured_jd": structured_jd,
        "job_data_urls": [{"job_id": str(job_id), "url": job_data_url}] if job_data_url else [],
    }



