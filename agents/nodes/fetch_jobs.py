from typing import Dict, Any
from datetime import datetime
import uuid

# Simple imports
import sys
sys.path.append("..")

from agents.db import job_collection_name
from agents.nodes.state import RecruitmentState


async def fetch_pending_jobs_from_db(state: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch pending jobs - Simple and Reliable version"""
    
    print("🔍 Fetching pending jobs from database...")   # ← Added for debugging

    # Simpler and more reliable query
    cursor_jobs = job_collection_name.find({
        "$or": [
            {"processed": False},           # explicitly false
            {"processed": {"$exists": False}}  # or field doesn't exist
        ]
    }).sort("scraped_at", -1).limit(2)

    jobs = await cursor_jobs.to_list(length=100)

    pending_jobs_id = [str(job["_id"]) for job in jobs]

    print(f"Found {len(jobs)} pending jobs")   

    if jobs:
        print(f"First job title: {jobs[0].get('title', 'No title')}")

    return {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now().isoformat(),
        "pending_jobs_id": pending_jobs_id,
        "current_job_index": 0,
    }