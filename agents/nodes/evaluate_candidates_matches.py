from typing import Dict, Any, List
import sys
sys.path.append("..")
import json

from agents.db import bench_resume_collection, bench_candidate_evaluation
from .state import RecruitmentState


# ============== LLM SETUP ==============
from langchain_openai import ChatOpenAI
# from langchain_groq import ChatGroq   # use this if you prefer Grok

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)   # fast & cheap for matching


MATCH_PROMPT = """
You are an expert IT recruiter with 15+ years experience.

Job Details:
{job_details}

Candidate Resume:
{resume_text}

Analyze how well this candidate fits the job.
Return ONLY valid JSON with these exact keys:

{{
  "match_score": number between 0 and 100,
  "shortlisted": true or false,
  "reasoning": "clear 2-3 sentence explanation why this score and decision",
  "key_strengths": ["list", "of", "strengths"],
  "key_gaps": ["list", "of", "gaps"]
}}

Be honest and strict. Only shortlist if the candidate is genuinely strong (score >=50).
"""

from langchain_core.prompts import ChatPromptTemplate
prompt_template = ChatPromptTemplate.from_template(MATCH_PROMPT)


async def evaluate_candidate_matches(state: Dict[str, Any]) -> Dict[str, Any]:
    """Node 3: Full LLM matching - complete job vs complete resume"""
    
    structured_jd = state.get("structured_jd")
    if not structured_jd:
        print("⚠️ No job data to match against.")
        return state

    cursor = bench_resume_collection.find({
        "is_bench": "yes",
    })
    all_candidates = await cursor.to_list(length=None)

    if not all_candidates:
        print("⚠️ No bench resume found (is_bench: yes) — skipping evaluation.")
        return {"evaluations": [], "shortlisted_candidates": []}

    print(f"🔥 Starting full matching for today's candidate: {all_candidates[0].get('name', 'Unknown')}...")

    evaluations = []
    shortlisted_list = []

    job_summary = json.dumps({
        "title": structured_jd.get("job_title"),
        "company": structured_jd.get("company"),
        "skills": structured_jd.get("required_skills"),
        "location": structured_jd.get("location"),
        "type": structured_jd.get("job_type"),
        "experience": structured_jd.get("experience"),
        "salary": structured_jd.get("salary"),
        "responsibilities": structured_jd.get("responsibilities", [])[:8],
        "qualifications": structured_jd.get("qualifications", [])[:6],
    }, ensure_ascii=False)

    for candidate in all_candidates:
        candidate_id = str(candidate["_id"])
        name = candidate.get("name", "Unknown")
        email = candidate.get("email", "")

        # Flatten skills — bench_candidates_resume stores skills as a nested dict
        raw_skills = candidate.get("skills", {})
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

        resume_text = f"""
Name: {candidate.get('name')}
Location: {candidate.get('location', '')}
Visa Status: {candidate.get('visa_status', '')}
Total Experience: {candidate.get('total_experience_years', 0)} years
Current Title: {candidate.get('current_title', '')}
Summary: {candidate.get('summary', '')}
Skills: {json.dumps(flat_skills)}
Experience: {json.dumps(candidate.get('experience', []))}
Education: {json.dumps(candidate.get('education', []))}
Certifications: {json.dumps(candidate.get('certifications', []))}
        """.strip()

        try:
            chain = prompt_template | llm
            response = await chain.ainvoke({
                "job_details": job_summary,
                "resume_text": resume_text
            })

            content = response.content.strip()
            # Clean JSON if needed
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif content.startswith("```"):
                content = content.split("```")[1].strip()

            result = json.loads(content)

            match_score = result.get("match_score", 0)
            shortlisted = match_score >= 50

            eval_record = {
                "job_id": state["current_job_id"],
                "job_title": structured_jd.get("job_title", ""),
                "posted_date": structured_jd.get("posted_date", ""),
                "job_url": structured_jd.get("job_url", ""),
                "candidate_id": candidate_id,
                "resume_id": candidate_id,
                "candidate_name": name,
                "candidate_email": email,
                "match_score": match_score,
                "shortlisted": shortlisted,
                "reasoning": result.get("reasoning", ""),
                "key_strengths": result.get("key_strengths", []),
                "key_gaps": result.get("key_gaps", []),
                "evaluated_at": state.get("timestamp"),
                "original_resume_s3_url": candidate.get("resume_url", ""),
                "formatted_shortlisted_resume_s3_url": "",
                "tuned_formatted_shortlisted_resume_s3_url": "",
            }

            evaluations.append(eval_record)

            if shortlisted:
                shortlisted_list.append(eval_record)

            print(f"   ✓ {name} → Score: {match_score} | Shortlisted: {shortlisted}")

        except Exception as e:
            print(f"   ✗ Error evaluating {name}: {e}")
            continue

    # Save evaluations to database
    if evaluations:
        await bench_candidate_evaluation.insert_many(evaluations)

    print(f"\n🎯 Matching completed! Shortlisted: {len(shortlisted_list)} candidates")

    return {
        "evaluations": evaluations,
        "shortlisted_candidates": shortlisted_list,
    }


