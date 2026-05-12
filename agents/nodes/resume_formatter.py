"""
Resume Formatter Node

Picks up each shortlisted candidate's raw resume from MongoDB,
converts it into a standardised structured JSON format via LLM,
and uploads the result to S3.

This node is CONTENT-PRESERVING: every piece of information from the
original resume is kept exactly as-is. No additions, no removals.

Structured format produced:
{
  "candidate_profile_details": { name, email, phone, linkedin_id },
  "profile_summary": [ ...bullet strings from the original summary... ],
  "skills": { "Programming Languages": [...], "Frameworks": [...], ... },
  "education": "Degree, Institution – Month Year",
  "work_experience": [
    {
      "employer_name",      <- who employs the candidate (e.g. TCS, Infosys)
      "client_name",        <- end-client they serve (e.g. MetLife, Humana) or ""
      "client_location",
      "Candidate_designation",
      "time_frame",         <- full range e.g. "01/2024 – Present"
      "project_description",
      "responsibilities": [...],
      "environment"
    }
  ]
}
"""

import json
from typing import Dict, Any

from bson import ObjectId
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from agents.db import bench_resume_collection
from agents.utils.s3_utils import upload_resume_json, formatted_s3_key, generate_presigned_url
from agents.nodes.validate_resume import (
    validate_formatted_resume,
    build_formatter_retry_prompt,
)

llm = ChatOpenAI(model="gpt-4o", temperature=0)

FORMAT_PROMPT = """
You are an expert resume formatter for US IT staffing.

Your ONLY job is to convert the raw resume text below into the exact JSON structure shown.
You must preserve 100% of the original content — do NOT remove, skip, summarize, or alter anything.

━━━ STEP 1 — COUNT ━━━
Before writing the JSON, count every distinct work experience entry in the raw resume.
Each entry begins with a date range (e.g. "01/2024 to Current", "05/2023 to 12/2023").
Record that count as the integer value of "work_experience_count".

━━━ STEP 2 — FORMAT ━━━
Convert each entry to a work_experience object preserving:
• employer_name  — the company that EMPLOYS the candidate (who pays their salary).
                   For US IT consulting resumes this is usually the staffing company
                   (e.g. "Tata Consultancy Services", "Infosys", "Wipro").
• client_name    — the end-client they were assigned to work at
                   (e.g. "MetLife Services and Solutions LLC", "Humana / CenterWell Pharmacy").
                   Use empty string "" for direct-hire roles with no client.
• time_frame     — the FULL date range: "MM/YYYY – MM/YYYY" or "MM/YYYY – Present".
• responsibilities — every bullet point from the original. Do not summarise or drop any.

━━━ RAW RESUME ━━━
{raw_resume}

━━━ REQUIRED OUTPUT (valid JSON only — no markdown fences, no extra text) ━━━
{{
  "work_experience_count": <integer — total jobs counted above>,
  "candidate_profile_details": {{
    "name": "Full Name",
    "email": "email@example.com",
    "phone": "phone number or empty string",
    "linkedin_id": "LinkedIn URL or empty string"
  }},
  "profile_summary": [
    "Achievement-focused bullet preserving the original summary content",
    "... (include ALL summary points from the original, 6–10 bullets)"
  ],
  "skills": {{
    "Programming Languages": ["Java", "Python"],
    "Frameworks": ["Spring Boot", "Hibernate"],
    "Web Technologies": ["REST APIs", "Servlets"],
    "Databases": ["Oracle", "IBM DB2"],
    "Cloud & DevOps": ["Azure", "Docker", "Jenkins"],
    "Version Control": ["Git", "GitHub"],
    "Other Skills": ["CI/CD", "Microservices", "Agile"]
  }},
  "education": "Bachelor of Engineering in Computer Science, University Name, City – MM/YYYY",
  "work_experience": [
    {{
      "employer_name": "Tata Consultancy Services",
      "client_name": "MetLife Services and Solutions LLC",
      "client_location": "Cary, NC, USA",
      "Candidate_designation": "Technical Lead",
      "time_frame": "01/2024 – Present",
      "project_description": "One-line description of the project or role",
      "responsibilities": [
        "Exact responsibility bullet 1 from the original resume",
        "Exact responsibility bullet 2"
      ],
      "environment": "Java, Spring Boot, Azure DevOps"
    }}
  ]
}}

━━━ STRICT RULES ━━━
1. work_experience array MUST contain exactly work_experience_count entries — no more, no less.
2. List work_experience in reverse chronological order (most recent job FIRST).
3. employer_name is the staffing/employer company; client_name is the end-client.
4. Include EVERY work experience entry — including the most recent ongoing role.
5. Only include skill categories that have actual values from the resume.
6. Preserve the candidate's original responsibility wording — do not rewrite or shorten.
7. Use empty string "" for any unknown field — never null.
"""

prompt_template = ChatPromptTemplate.from_template(FORMAT_PROMPT)

MAX_FORMAT_RETRIES = 2


async def _get_raw_resume_text(doc: dict) -> str:
    """
    Use raw_text stored at upload time as the source of truth.
    Fall back to building from DB fields only if raw_text is absent (legacy records).
    """
    raw_text = doc.get("raw_text", "")
    if raw_text:
        return raw_text

    raw_skills = doc.get("skills", {})
    if isinstance(raw_skills, dict):
        flat_skills = (
            raw_skills.get("primary_skills", []) +
            raw_skills.get("secondary_skills", []) +
            raw_skills.get("cloud_devops", []) +
            raw_skills.get("databases", []) +
            raw_skills.get("tools", [])
        )
    else:
        flat_skills = raw_skills if isinstance(raw_skills, list) else []

    return json.dumps({
        "name": doc.get("name", ""),
        "email": doc.get("email", ""),
        "phone": doc.get("phone", ""),
        "location": doc.get("location", ""),
        "linkedin": doc.get("linkedin", doc.get("linkedin_id", "")),
        "summary": doc.get("summary", ""),
        "skills": flat_skills,
        "experience": doc.get("experience", []),
        "education": doc.get("education", []),
        "certifications": doc.get("certifications", []),
    }, ensure_ascii=False)


def _parse_llm_json(content: str) -> dict:
    content = content.strip()
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0].strip()
    elif content.startswith("```"):
        content = content.split("```")[1].strip()
    return json.loads(content)


async def _format_with_retry(chain, raw_text: str, candidate_name: str) -> dict:
    """
    Call the formatter LLM, validate the output, and retry once with a
    corrective prompt if validation fails.
    """
    input_text = raw_text

    for attempt in range(MAX_FORMAT_RETRIES):
        response = await chain.ainvoke({"raw_resume": input_text})
        try:
            structured = _parse_llm_json(response.content)
        except json.JSONDecodeError as e:
            print(f"   [{candidate_name}] JSON parse error on attempt {attempt + 1}: {e}")
            if attempt < MAX_FORMAT_RETRIES - 1:
                continue
            raise

        is_valid, errors = validate_formatted_resume(structured, raw_text)

        if is_valid:
            if attempt > 0:
                print(f"   [{candidate_name}] Validation passed on retry {attempt + 1}.")
            return structured

        print(f"   [{candidate_name}] Validation failed (attempt {attempt + 1}):")
        for err in errors:
            print(f"      • {err}")

        if attempt < MAX_FORMAT_RETRIES - 1:
            print(f"   [{candidate_name}] Retrying with corrective instructions...")
            input_text = build_formatter_retry_prompt(raw_text, errors)
        else:
            print(
                f"   [{candidate_name}] WARNING: Validation still failing after "
                f"{MAX_FORMAT_RETRIES} attempts. Accepting best-effort output."
            )

    return structured


async def format_shortlisted_resumes(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node: For every shortlisted candidate in the current job,
    fetch their raw resume from DB, convert to structured JSON via LLM
    (with validation + retry), and upload the result to S3.
    """
    current_job_id = state.get("current_job_id", "")
    all_shortlisted = state.get("shortlisted_candidates", [])
    current_shortlisted = [c for c in all_shortlisted if c.get("job_id") == current_job_id]

    if not current_shortlisted:
        print("   No shortlisted candidates — skipping resume formatting.")
        return {"formatted_resume_data": []}

    print(f"\n📄 Formatting resumes for {len(current_shortlisted)} shortlisted candidate(s)...")

    chain = prompt_template | llm
    results = []

    for candidate_eval in current_shortlisted:
        candidate_id = candidate_eval.get("candidate_id")
        name = candidate_eval.get("candidate_name", "Unknown")

        try:
            doc = await bench_resume_collection.find_one({"_id": ObjectId(candidate_id)})
        except Exception:
            doc = await bench_resume_collection.find_one({"_id": candidate_id})

        if not doc:
            print(f"   WARNING: Resume doc not found for {name} ({candidate_id}) — skipping.")
            continue

        raw_text = await _get_raw_resume_text(doc)
        if not raw_text:
            print(f"   WARNING: No raw_text found for {name} — skipping.")
            continue

        try:
            structured = await _format_with_retry(chain, raw_text, name)
        except Exception as e:
            print(f"   ERROR formatting resume for {name}: {e}")
            continue

        key = formatted_s3_key(current_job_id, name)
        s3_uri = upload_resume_json(structured, key)
        presigned_url = generate_presigned_url(key) if s3_uri else None

        results.append({
            "candidate_id": candidate_id,
            "candidate_name": name,
            "job_id": current_job_id,
            "formatted_s3_key": key,
            "formatted_s3_uri": s3_uri,
            "formatted_presigned_url": presigned_url,
            "formatted_resume": structured,
        })
        print(f"   ✓ Formatted: {name} → {s3_uri}")

    return {"formatted_resume_data": results}
