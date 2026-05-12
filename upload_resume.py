"""
Bench Candidate Resume Upload API

POST /upload-bench-resume
  - Accepts PDF or DOCX resume from IT recruiter
  - Uploads original file to S3 (bench-candidates bucket, original_resume/ subfolder)
  - Extracts raw text and parses structured flags via OpenAI GPT-4o
  - Stores full record in MongoDB: IT_Jobs.bench_candidates_resume
"""

import json
import os
import uuid
from datetime import datetime, timezone
from io import BytesIO

import boto3
import pdfplumber
from docx import Document as DocxDocument
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from openai import OpenAI

from agents.config import load_secrets
from agents.db import bench_resume_collection

load_secrets()

app = FastAPI(title="IT Sourcing – Bench Resume Upload API", version="1.0.0")

S3_BUCKET = "bench-candidates"
S3_REGION = "ap-south-1"
S3_SUBFOLDER = "original_resume"

PARSE_PROMPT = """\
You are an expert IT recruiter and resume parser for US IT staffing.

Extract ALL information from the resume text below into this EXACT JSON structure.
Return ONLY valid JSON — no markdown fences, no extra text.


Resume Text:
{raw_text}

Required output structure:
{{
  "name": "Full Name",
  "email": "email@example.com",
  "phone": "+1-XXX-XXX-XXXX",
  "location": "City, State",
  "linkedin": "LinkedIn URL or empty string",
  "visa_status": "H1B / GC / USC / OPT / CPT / TN / Other — or Nan if not found",
  "availability": "Immediate / 2 weeks / 1 month / specific date — or empty string",
  "total_experience_years": 5,
  "current_employer": "Most recent company name or empty string",
  "current_title": "Most recent job title or empty string",
  "skills": {{
    "primary_skills": ["list of main technical skills"],
    "secondary_skills": ["supporting/secondary skills"],
    "cloud_devops": ["AWS", "Azure", "Docker", "Kubernetes"],
    "databases": ["MySQL", "MongoDB", "PostgreSQL"],
    "tools": ["Jira", "Git", "Jenkins", "Postman"]
  }},
  "experience": [
    {{
      "company": "Company Name",
      "title": "Job Title",
      "client": "Client Name or Nan",
      "start_date": "MM/YYYY",
      "end_date": "MM/YYYY or Present",
      "duration": "X years Y months",
      "location": "City, State or Remote",
      "responsibilities": ["bullet 1", "bullet 2"],
      "technologies": ["tech1", "tech2"]
    }}
  ],
  "education": [
    {{
      "degree": "Bachelor of Science in Computer Science",
      "institution": "University Name",
      "location": "City, State",
      "graduation_year": "2020"
    }}
  ],
  "certifications": ["AWS Certified Solutions Architect", "..."],
  "summary": "Professional summary paragraph as-is from the resume"
}}

Rules:
- List experience in reverse chronological order (most recent first).
- If a field is not found use: "" for strings, 0 for numbers, [] for arrays.
- Detect visa_status from context clues: "US Citizen", "Green Card Holder", "H1B", "GC", "EAD", "OPT", etc. If not found give "Nan".
- Extract EVERY piece of information — do not skip certifications, tools, or project details.
- Keep responsibilities as individual bullet strings, not one long paragraph.
- Dont shrink his original resume or miss any information.
"""


def _s3_client():
    return boto3.client("s3", region_name=S3_REGION)


def _upload_to_s3(file_bytes: bytes, original_filename: str, content_type: str) -> str:
    safe_name = original_filename.replace(" ", "_")
    key = f"{S3_SUBFOLDER}/{uuid.uuid4()}_{safe_name}"
    _s3_client().put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=file_bytes,
        ContentType=content_type,
        ContentDisposition="inline",
    )
    return f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{key}"


def _extract_text_pdf(file_bytes: bytes) -> str:
    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    return "\n".join(pages).strip()


def _extract_text_docx(file_bytes: bytes) -> str:
    doc = DocxDocument(BytesIO(file_bytes))
    parts = []
    seen_cell_ids: set = set()

    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text.strip())

    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                cid = id(cell._tc)
                if cid in seen_cell_ids:
                    continue
                seen_cell_ids.add(cid)
                text = cell.text.strip()
                if text:
                    parts.append(text)

    return "\n".join(parts).strip()


def _parse_with_llm(raw_text: str) -> dict:
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model="gpt-5.4-mini",
        temperature=0,
        messages=[{"role": "user", "content": PARSE_PROMPT.format(raw_text=raw_text)}],
    )
    content = response.choices[0].message.content.strip()
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0].strip()
    elif content.startswith("```"):
        content = content.split("```")[1].strip()
    return json.loads(content)


@app.post("/upload-bench-resume", status_code=201)
async def upload_bench_resume(
    file: UploadFile = File(...),
):
    """
    Upload a bench candidate resume (PDF or DOCX).
    Returns candidate_id, name, and the public S3 URL of the uploaded file.
    """
    filename = file.filename or "resume"
    content_type = file.content_type or ""

    is_pdf = filename.lower().endswith(".pdf") or "pdf" in content_type
    is_docx = filename.lower().endswith(".docx") or "wordprocessingml" in content_type

    if not (is_pdf or is_docx):
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Only PDF and DOCX resumes are accepted.",
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # 1. Extract raw text
    try:
        if is_pdf:
            raw_text = _extract_text_pdf(file_bytes)
            ct = "application/pdf"
        else:
            raw_text = _extract_text_docx(file_bytes)
            ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Text extraction failed: {exc}")

    if not raw_text:
        raise HTTPException(
            status_code=422,
            detail="No readable text found in the uploaded file. Ensure it is not a scanned image.",
        )

    # 2. Upload original file to S3
    try:
        resume_url = _upload_to_s3(file_bytes, filename, ct)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"S3 upload failed: {exc}")

    # 3. Parse resume flags with OpenAI
    try:
        parsed = _parse_with_llm(raw_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"LLM returned invalid JSON: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"LLM parsing failed: {exc}")

    # 4. Build the full record
    record = {
        **parsed,
        "raw_text": raw_text,
        "resume_url": resume_url,
        "is_bench": "yes",
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }

    # 5. Store in MongoDB IT_Jobs.bench_candidates_resume
    try:
        result = await bench_resume_collection.insert_one(record)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database insert failed: {exc}")

    return JSONResponse(
        status_code=201,
        content={
            "message": "Resume uploaded and parsed successfully.",
            "candidate_id": str(result.inserted_id),
            "candidate_name": parsed.get("name", ""),
            "resume_url": resume_url,
        },
    )
