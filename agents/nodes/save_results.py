from typing import Dict, Any
import json
import os
from datetime import datetime
from bson import ObjectId

from agents.db import job_collection_name


async def save_results_to_files(state: Dict[str, Any]) -> Dict[str, Any]:
    """Node 4: Save evaluation results to agent_resume_results/ directory"""

    job_id = state.get("current_job_id", "unknown")

    # Filter evaluations for current job only (state accumulates across all jobs)
    all_evaluations = state.get("evaluations", [])
    evaluations = [e for e in all_evaluations if e.get("job_id") == job_id]

    if not evaluations:
        print("⚠️ No evaluations to save for this job.")
        return {"processed_jobs": [job_id], "current_job_index": state.get("current_job_index", 0) + 1}

    structured_jd = state.get("structured_jd", {})
    job_title = structured_jd.get("job_title", "unknown_job")
    company = structured_jd.get("company", "unknown_company")
    run_id = state.get("run_id", "unknown")
    timestamp = state.get("timestamp", datetime.now().isoformat())

    # Create output directory
    base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "agent_resume_results")
    # Sub-folder per job run: agent_resume_results/<job_id>_<timestamp>/
    safe_title = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in job_title)
    run_folder = f"{safe_title}_{job_id[:8]}"
    output_dir = os.path.join(base_dir, run_folder)
    os.makedirs(output_dir, exist_ok=True)

    print(f"\n📁 Saving results to: {output_dir}")

    # --- 1. Save individual candidate result files ---
    for eval_record in evaluations:
        candidate_name = eval_record.get("candidate_name", "unknown")
        safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in candidate_name)
        filename = f"{safe_name}.json"

        result_data = {
            "candidate_name": candidate_name,
            "candidate_id": eval_record.get("candidate_id"),
            "match_score": eval_record.get("match_score", 0),
            "shortlisted": eval_record.get("shortlisted", False),
            "reasoning": eval_record.get("reasoning", ""),
            "key_strengths": eval_record.get("key_strengths", []),
            "key_gaps": eval_record.get("key_gaps", []),
            "job_title": job_title,
            "company": company,
            "job_id": eval_record.get("job_id"),
            "evaluated_at": eval_record.get("evaluated_at"),
        }

        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(result_data, f, indent=2, ensure_ascii=False)

        status = "✅ SHORTLISTED" if result_data["shortlisted"] else "—"
        print(f"   💾 {candidate_name}: score={result_data['match_score']} {status}")

    # --- 2. Save summary file with all candidates ranked ---
    sorted_evals = sorted(evaluations, key=lambda x: x.get("match_score", 0), reverse=True)

    summary = {
        "run_id": run_id,
        "timestamp": timestamp,
        "job_title": job_title,
        "company": company,
        "job_id": job_id,
        "total_candidates": len(evaluations),
        "shortlisted_count": sum(1 for e in evaluations if e.get("shortlisted")),
        "candidates": [
            {
                "rank": i + 1,
                "candidate_name": e.get("candidate_name"),
                "candidate_id": e.get("candidate_id"),
                "match_score": e.get("match_score", 0),
                "shortlisted": e.get("shortlisted", False),
                "reasoning": e.get("reasoning", ""),
            }
            for i, e in enumerate(sorted_evals)
        ],
    }

    summary_path = os.path.join(output_dir, "_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"\n📊 Summary saved: {summary_path}")
    print(f"   Total: {summary['total_candidates']} candidates | Shortlisted: {summary['shortlisted_count']}")

    # Mark job as processed in the database using the MongoDB _id
    index = state.get("current_job_index", 0)
    mongo_id = state["pending_jobs_id"][index]
    await job_collection_name.update_one(
        {"_id": ObjectId(mongo_id)},
        {"$set": {"processed": True}}
    )
    print(f"   ✅ Job {job_id} marked as processed in DB")

    return {
        "processed_jobs": [job_id],
        "current_job_index": index + 1,
    }
