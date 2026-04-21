from typing import Dict, Any, List
import json

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from agents.db import resume_collection_name, tuned_resumes_collection
from agents.utils.s3_utils import upload_resume_json, tuned_s3_key, generate_presigned_url


llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3)


# ============== PROMPT 1: Tune the resume ==============
TUNE_PROMPT = """
You are an expert IT resume writer and career coach with 15+ years of experience.

Your job is to REWRITE a candidate's resume to better match a specific Job Description (JD).

STRICT RULES — NEVER violate these:
1. Do NOT change or invent: candidate name, email, phone, location, education (degrees, institutions, graduation dates), company names, job titles, employment dates, certifications names.
2. You CAN improve: professional summary, skills list (add relevant skills the candidate likely has based on their experience), experience bullet points (rephrase to highlight JD-relevant keywords), add missing JD keywords naturally.
3. The goal is to make the resume PASS ATS (Applicant Tracking Systems) for this specific JD.
4. Do not fabricate experience or skills that have zero basis in the original resume.

Job Description:
{job_details}

Original Resume:
{original_resume}

Return ONLY valid JSON with this exact structure:
{{
  "summary": "improved professional summary (2-4 sentences, JD-aligned)",
  "skills": ["skill1", "skill2", ...],
  "experience": [
    {{
      "company": "same company name",
      "title": "same job title",
      "dates": "same dates",
      "bullets": ["rewritten bullet 1", "rewritten bullet 2", ...]
    }}
  ],
  "improvements_made": ["list of specific improvements made", "e.g. Added Spring Boot to skills", "Reframed experience to highlight microservices"]
}}
"""

# ============== PROMPT 2: Re-score the tuned resume ==============
RESCORE_PROMPT = """
You are an expert IT recruiter with 15+ years experience.

Job Details:
{job_details}

Candidate Resume (TUNED VERSION):
{resume_text}

Analyze how well this TUNED candidate resume fits the job.
Return ONLY valid JSON with these exact keys:

{{
  "match_score": number between 0 and 100,
  "shortlisted": true or false,
  "reasoning": "clear 2-3 sentence explanation",
  "key_strengths": ["list", "of", "strengths"],
  "key_gaps": ["list", "of", "gaps"]
}}

Be honest. Only shortlist if score >= 40.
"""

STRUCTURE_TUNED_PROMPT = """
You are an expert resume writer.

Below is a candidate's tuned resume data (produced by an AI tuner) and their original profile details.
Convert everything into this EXACT structured JSON format.
Return ONLY valid JSON — no markdown fences, no extra text.

Original profile details (do NOT change name, email, linkedin, education, company names, titles, dates):
{original_details}

AI-tuned resume content:
{tuned_content}

Required output structure:
{{
  "candidate_profile_details": {{
    "name": "Full Name",
    "email": "email@example.com",
    "linkedin_id": "linkedin URL or empty string"
  }},
  "profile_summary": [
    "Tuned achievement-focused bullet 1",
    "Tuned bullet 2",
    "... (6-10 bullets from the tuned summary)"
  ],
  "skills": {{
    "Programming Languages": ["..."],
    "Frameworks": ["..."],
    "Web Technologies": ["..."],
    "Databases": ["..."],
    "Cloud & DevOps": ["..."],
    "Version Control": ["..."],
    "Other Skills": ["..."]
  }},
  "education": "Degree, Institution, City – Month Year",
  "work_experience": [
    {{
      "client_name": "Same company name as original",
      "client_location": "City, Country",
      "Candidate_designation": "Same job title as original or Nan",
      "time_frame": "Month/Year (same as original)",
      "project_description": "One-line description of the role",
      "responsibilities": [
        "Tuned responsibility bullet 1",
        "Tuned responsibility bullet 2",
        "... (4-6 bullets from tuned experience)"
      ],
      "environment": "Tech stack comma separated"
    }}
  ]
}}

Rules:
- Only include skill categories that have actual values.
- List work_experience in reverse chronological order.
- If a field value is unknown, use empty string "".
"""

tune_prompt_template = ChatPromptTemplate.from_template(TUNE_PROMPT)
rescore_prompt_template = ChatPromptTemplate.from_template(RESCORE_PROMPT)
structure_tuned_prompt_template = ChatPromptTemplate.from_template(STRUCTURE_TUNED_PROMPT)


def _parse_llm_json(content: str) -> dict:
    content = content.strip()
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0].strip()
    elif content.startswith("```"):
        content = content.split("```")[1].strip()
    return json.loads(content)


async def tune_shortlisted_resumes(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Node 3b: For each shortlisted candidate in the current job,
    tune their resume against the JD, then re-score it.
    Returns tuned_candidates list with before/after scores.
    """

    structured_jd = state.get("structured_jd")
    current_job_id = state.get("current_job_id")

    if not structured_jd or not current_job_id:
        return {"tuned_candidates": []}

    # Only process shortlisted candidates for the CURRENT job
    all_shortlisted = state.get("shortlisted_candidates", [])
    current_shortlisted = [c for c in all_shortlisted if c.get("job_id") == current_job_id]

    if not current_shortlisted:
        print("   No shortlisted candidates for this job — skipping tuning.")
        return {"tuned_candidates": []}

    print(f"\n✏️  Tuning {len(current_shortlisted)} shortlisted resume(s) for: {structured_jd.get('job_title')}")

    job_summary = json.dumps({
        "title": structured_jd.get("job_title"),
        "company": structured_jd.get("company"),
        "skills": structured_jd.get("required_skills"),
        "location": structured_jd.get("location"),
        "type": structured_jd.get("job_type"),
        "experience": structured_jd.get("experience"),
        "responsibilities": structured_jd.get("responsibilities", [])[:10],
        "qualifications": structured_jd.get("qualifications", [])[:6],
        "description": structured_jd.get("description", "")[:2000],
    }, ensure_ascii=False)

    tuned_candidates = []

    for candidate in current_shortlisted:
        candidate_id = candidate["candidate_id"]
        name = candidate["candidate_name"]
        before_score = candidate["match_score"]

        print(f"\n   [{name}] Before score: {before_score}")

        # Fetch full resume document from MongoDB
        from bson import ObjectId
        resume_doc = await resume_collection_name.find_one({"_id": ObjectId(candidate_id)})
        if not resume_doc:
            print(f"   WARNING: Resume not found for {name}, skipping.")
            continue

        original_resume = json.dumps({
            "name": resume_doc.get("name"),
            "summary": resume_doc.get("summary", ""),
            "skills": resume_doc.get("skills", []),
            "experience": resume_doc.get("experience", []),
            "education": resume_doc.get("education", []),
            "certifications": resume_doc.get("certifications", []),
        }, ensure_ascii=False)

        # ---- STEP 1: Tune the resume ----
        try:
            tune_chain = tune_prompt_template | llm
            tune_response = await tune_chain.ainvoke({
                "job_details": job_summary,
                "original_resume": original_resume,
            })
            tuned = _parse_llm_json(tune_response.content)
        except Exception as e:
            print(f"   ERROR tuning {name}: {e}")
            continue

        improvements = tuned.get("improvements_made", [])
        print(f"   [{name}] Tuning done. Improvements: {len(improvements)}")

        # ---- STEP 2: Re-score the tuned resume ----
        # Build tuned resume text (keep education/location from original)
        tuned_resume_text = f"""
Name: {resume_doc.get('name')}
Location: {resume_doc.get('location', resume_doc.get('address', ''))}
Email: {resume_doc.get('email', '')}

Summary: {tuned.get('summary', '')}

Skills: {json.dumps(tuned.get('skills', []))}

Experience:
{chr(10).join(
    f"  {exp.get('title')} at {exp.get('company')} ({exp.get('dates', '')})" +
    chr(10) + chr(10).join(f"    - {b}" for b in exp.get('bullets', []))
    for exp in tuned.get('experience', [])
)}

Education: {json.dumps(resume_doc.get('education', []))}
Certifications: {json.dumps(resume_doc.get('certifications', []))}
        """.strip()

        try:
            rescore_chain = rescore_prompt_template | llm
            rescore_response = await rescore_chain.ainvoke({
                "job_details": job_summary,
                "resume_text": tuned_resume_text,
            })
            rescored = _parse_llm_json(rescore_response.content)
            after_score = rescored.get("match_score", before_score)
        except Exception as e:
            print(f"   ERROR re-scoring {name}: {e}")
            after_score = before_score
            rescored = {}

        print(f"   [{name}] After score: {after_score} (gain: +{after_score - before_score})")

        # ---- STEP 3: Convert tuned content to structured format and upload to S3 ----
        tuned_s3_uri = None
        tuned_presigned_url = None
        structured_tuned_resume = {}
        try:
            original_details = json.dumps({
                "name": resume_doc.get("name"),
                "email": resume_doc.get("email", ""),
                "linkedin": resume_doc.get("linkedin", resume_doc.get("linkedin_id", "")),
                "education": resume_doc.get("education", []),
                "location": resume_doc.get("location", resume_doc.get("address", "")),
            }, ensure_ascii=False)

            structure_chain = structure_tuned_prompt_template | llm
            structure_response = await structure_chain.ainvoke({
                "original_details": original_details,
                "tuned_content": json.dumps(tuned, ensure_ascii=False),
            })
            structured_tuned_resume = _parse_llm_json(structure_response.content)

            s3_key = tuned_s3_key(current_job_id, name)
            tuned_s3_uri = upload_resume_json(structured_tuned_resume, s3_key)
            tuned_presigned_url = generate_presigned_url(s3_key) if tuned_s3_uri else None
            print(f"   [{name}] Tuned structured resume uploaded → {tuned_s3_uri}")
        except Exception as e:
            print(f"   WARNING: Could not structure/upload tuned resume for {name}: {e}")

        tuned_record = {
            "job_id": current_job_id,
            "job_title": structured_jd.get("job_title", ""),
            "posted_date": structured_jd.get("posted_date", ""),
            "candidate_id": candidate_id,
            "candidate_name": name,
            "candidate_email": candidate.get("candidate_email", ""),
            "before_score": before_score,
            "after_score": after_score,
            "score_gain": after_score - before_score,
            "improvements_made": improvements,
            # Keep structured data in-memory for email rendering
            "tuned_resume": {
                "summary": tuned.get("summary", ""),
                "skills": tuned.get("skills", []),
                "experience": tuned.get("experience", []),
                "education": resume_doc.get("education", []),
                "certifications": resume_doc.get("certifications", []),
                "location": resume_doc.get("location", resume_doc.get("address", "")),
            },
            "tuned_resume_text": tuned_resume_text,
            "before_reasoning": candidate.get("reasoning", ""),
            "after_reasoning": rescored.get("reasoning", ""),
            "after_strengths": rescored.get("key_strengths", []),
            "after_gaps": rescored.get("key_gaps", []),
            "evaluated_at": state.get("timestamp"),
            # S3 links for the structured tuned resume
            "tuned_s3_uri": tuned_s3_uri,
            "tuned_presigned_url": tuned_presigned_url,
            "structured_tuned_resume": structured_tuned_resume,
        }

        tuned_candidates.append(tuned_record)

        # Save to MongoDB — store tuned resume as full text string, not structured dict
        await tuned_resumes_collection.insert_one({
            "job_id": current_job_id,
            "job_title": structured_jd.get("job_title", ""),
            "candidate_id": candidate_id,
            "candidate_name": name,
            "candidate_email": candidate.get("candidate_email", ""),
            "before_score": before_score,
            "after_score": after_score,
            "score_gain": after_score - before_score,
            "improvements_made": improvements,
            "tuned_resume_text": tuned_resume_text,
            "before_reasoning": candidate.get("reasoning", ""),
            "after_reasoning": rescored.get("reasoning", ""),
            "evaluated_at": state.get("timestamp"),
        })

    print(f"\n✅ Resume tuning complete for this job. {len(tuned_candidates)} resume(s) tuned.")
    return {"tuned_candidates": tuned_candidates}
