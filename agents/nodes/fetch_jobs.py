from typing import Dict, Any
from datetime import datetime, timezone, timedelta
import uuid
import sys
sys.path.append("..")

from agents.db import job_collection_name
from agents.nodes.state import RecruitmentState


async def fetch_pending_jobs_from_db(state: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch all unprocessed jobs scraped in the last 24 hours."""

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    print(f"🔍 Fetching pending jobs scraped in the last 1 hour (since {cutoff.strftime('%Y-%m-%d %H:%M')} UTC)...")

    cursor_jobs = job_collection_name.find({
        "$or": [
            {"processed": "False"},
            {"processed": False},
        ],
        "scraped_at": {"$gte": cutoff},
    }).sort("scraped_at", -1)

    jobs = await cursor_jobs.to_list(length=None)
    pending_jobs_id = [str(job["_id"]) for job in jobs]

    print(f"Found {len(jobs)} pending job(s) in the last 1 hour")
    for job in jobs:
        scraped_at = job.get("scraped_at", "unknown date")
        print(f"  • {job.get('title', 'No title')} — scraped at {scraped_at}")

    return {
        "run_id": str(uuid.uuid4()),
        "timestamp": datetime.now().isoformat(),
        "pending_jobs_id": pending_jobs_id,
        "current_job_index": 0,
    }