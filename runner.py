import asyncio
import sys
import logging
import os
from datetime import datetime

# Fix Windows console encoding for emoji/unicode characters
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(override=True)

from agents.config import load_secrets
load_secrets()

from agents.graphs.graph import recruitment_app

# ============== LOGGING SETUP ==============
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

log_file = os.path.join(LOG_DIR, f"run_{datetime.now().strftime('%Y-%m-%d')}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
    force=True,
)
logger = logging.getLogger("recruitment_agent")


async def run_daily_recruitment_process():
    logger.info("Starting autonomous recruitment process...")

    initial_state = {}

    try:
        result = await recruitment_app.ainvoke(initial_state)
        logger.info("========== RUN COMPLETED ==========")

        logger.info(f'Run ID: {result.get("run_id")}')
        logger.info(f'Total Jobs Found: {len(result.get("pending_jobs_id", []))}')
        logger.info(f'Jobs Processed: {len(result.get("processed_jobs", []))}')
        logger.info(f'Processed Job IDs: {result.get("processed_jobs")}')

        evaluations = result.get("evaluations", [])
        shortlisted = result.get("shortlisted_candidates", [])
        logger.info(f'Total: {len(evaluations)} candidate evaluations across all jobs, {len(shortlisted)} shortlisted')
        logger.info(f'Results saved to: agent_resume_results/')

        email_sent = result.get("email_sent")
        if email_sent:
            logger.info("Email notification: SENT")
        else:
            logger.warning("Email notification: NOT SENT (check AWS SES credentials)")

        if result.get("errors"):
            logger.error(f'Errors: {result.get("errors")}')

        sys.exit(0)

    except Exception as e:
        logger.error(f"Error during recruitment process: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(run_daily_recruitment_process())
        


