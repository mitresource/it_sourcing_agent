from typing import Dict, Any
from bson import ObjectId
import sys
sys.path.append("..")

from agents.db import job_collection_name
from .state import RecruitmentState


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
        "original_job": job_doc,
    }

    print(f"✅ Job prepared: {structured_jd.get('job_title')} at {structured_jd.get('company')}")
    print(f"   Required skills: {len(structured_jd['required_skills'])} skills")

    return {
        "current_job_id": str(job_id),
        "current_job": job_doc,
        "raw_jd": job_doc.get("full_summary") or job_doc.get("raw_text"),
        "structured_jd": structured_jd,
    }



