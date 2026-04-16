from typing import Dict, Any, List
import sys
sys.path.append("..")
import json

from agents.db import resume_collection_name, evaluation_collection
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

Be honest and strict. Only shortlist if the candidate is genuinely strong (score >=40).
"""

from langchain_core.prompts import ChatPromptTemplate
prompt_template = ChatPromptTemplate.from_template(MATCH_PROMPT)


async def evaluate_candidate_matches(state: Dict[str, Any]) -> Dict[str, Any]:
    """Node 3: Full LLM matching - complete job vs complete resume"""
    
    structured_jd = state.get("structured_jd")
    if not structured_jd:
        print("⚠️ No job data to match against.")
        return state

    # Get ALL bench candidates (you can change limit later)
    cursor = resume_collection_name.find({"is_bench": "yes"}).limit(30)  # safety limit for now
    all_candidates = await cursor.to_list(length=50)

    print(f"🔥 Starting full matching for {len(all_candidates)} bench candidates...")

    evaluations = []
    shortlisted = []

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

        # Convert full resume to readable text
        resume_text = f"""
Name: {candidate.get('name')}
Summary: {candidate.get('summary', '')}
Skills: {json.dumps(candidate.get('skills', []))}
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

            eval_record = {
                "job_id": state["current_job_id"],
                "job_title": structured_jd.get("job_title", ""),
                "posted_date": structured_jd.get("posted_date", ""),
                "candidate_id": candidate_id,
                "resume_id": candidate_id,
                "candidate_name": name,
                "candidate_email": email,
                "match_score": result.get("match_score", 0),
                "shortlisted": result.get("shortlisted", False),
                "reasoning": result.get("reasoning", ""),
                "key_strengths": result.get("key_strengths", []),
                "key_gaps": result.get("key_gaps", []),
                "evaluated_at": state.get("timestamp"),
            }

            evaluations.append(eval_record)

            if result.get("shortlisted"):
                shortlisted.append(eval_record)

            print(f"   ✓ {name} → Score: {result.get('match_score')} | Shortlisted: {result.get('shortlisted')}")

        except Exception as e:
            print(f"   ✗ Error evaluating {name}: {e}")
            continue

    # Save evaluations to database
    if evaluations:
        await evaluation_collection.insert_many(evaluations)

    print(f"\n🎯 Matching completed! Shortlisted: {len(shortlisted)} candidates")

    return {
        "evaluations": evaluations,
        "shortlisted_candidates": shortlisted,
    }


