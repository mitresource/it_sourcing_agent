"""
Resume Formatter Node

Picks up each shortlisted candidate's raw resume from MongoDB
(uses resume_url field if present, otherwise builds from DB fields),
converts it into the standardised structured JSON format via LLM,
and uploads the result to S3.

Structured format produced:
{
  "candidate_profile_details": { name, email, linkedin_id },
  "profile_summary": [ ...bullet strings... ],
  "skills": { "Programming Languages": [...], "Frameworks": [...], ... },
  "education": "Degree, Institution – Month Year",
  "work_experience": [
    {
      "client_name", "client_location", "Candidate_designation",
      "time_frame", "project_description",
      "responsibilities": [...], "environment"
    }
  ]
}
"""

import json
from typing import Dict, Any

import aiohttp
from bson import ObjectId
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from agents.db import resume_collection_name
from agents.utils.s3_utils import upload_resume_json, formatted_s3_key, generate_presigned_url

llm = ChatOpenAI(model="gpt-4o", temperature=0)

FORMAT_PROMPT = """
You are an expert resume writer.

Convert the raw candidate resume data below into this EXACT JSON structure.
Return ONLY valid JSON — no markdown fences, no extra text.

Raw resume data:
{raw_resume}

Required output structure:
{{
  "candidate_profile_details": {{
    "name": "Full Name",
    "email": "email@example.com",
    "linkedin_id": "linkedin URL or empty string"
  }},
  "profile_summary": [
    "Concise achievement-focused bullet about background",
    "Bullet about key technical skills",
    "Bullet about domain experience",
    "... (6-10 bullets total)"
  ],
  "skills": {{
    "Programming Languages": ["Python", "Java"],
    "Frameworks": ["Django", "Spring Boot"],
    "Web Technologies": ["HTML", "CSS", "React"],
    "Databases": ["MySQL", "MongoDB"],
    "Cloud & DevOps": ["AWS", "Docker"],
    "Version Control": ["Git"],
    "Other Skills": ["REST APIs", "OOPs"]
  }},
  "education": "Master of Science in Computer Science, XYZ University, City – Month Year",
  "work_experience": [
    {{
      "client_name": "Company Name",
      "client_location": "City, Country",
      "Candidate_designation": "Job Title or Nan",
      "time_frame": "Month/Year",
      "project_description": "One-line description of the main project or role",
      "responsibilities": [
        "Responsibility bullet 1",
        "Responsibility bullet 2",
        "... (4-6 bullets)"
      ],
      "environment": "Python, Django, MongoDB, React"
    }}
  ]
}}

Rules:
- Only include skill categories that have actual values.
- List work_experience in reverse chronological order (most recent first).
- If a field value is unknown, use empty string "" (never null or None).
- Keep profile_summary bullets concise and achievement-focused.
"""

prompt_template = ChatPromptTemplate.from_template(FORMAT_PROMPT)


async def _get_raw_resume_text(doc: dict) -> str:
    """
    If the candidate has a resume_url, download the content from that URL.
    Otherwise compile from existing DB fields.
    """
    resume_url = doc.get("resume_url", "")
    if resume_url:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(resume_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        ct = resp.headers.get("Content-Type", "")
                        if "json" in ct:
                            return json.dumps(await resp.json(), ensure_ascii=False)
                        return await resp.text()
        except Exception as e:
            print(f"   WARNING: Could not fetch resume_url for {doc.get('name')}: {e}")

    # Fallback: build from DB fields
    return json.dumps({
        "name": doc.get("name", ""),
        "email": doc.get("email", ""),
        "phone": doc.get("phone", ""),
        "linkedin": doc.get("linkedin", doc.get("linkedin_id", "")),
        "summary": doc.get("summary", ""),
        "skills": doc.get("skills", []),
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


async def format_shortlisted_resumes(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node: For every shortlisted candidate in the current job,
    fetch their raw resume from DB/URL, convert to structured JSON via LLM,
    and upload the formatted version to S3.
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
            doc = await resume_collection_name.find_one({"_id": ObjectId(candidate_id)})
        except Exception:
            doc = await resume_collection_name.find_one({"_id": candidate_id})

        if not doc:
            print(f"   WARNING: Resume doc not found for {name} ({candidate_id}) — skipping.")
            continue

        raw_text = await _get_raw_resume_text(doc)

        try:
            response = await chain.ainvoke({"raw_resume": raw_text})
            structured = _parse_llm_json(response.content)
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
