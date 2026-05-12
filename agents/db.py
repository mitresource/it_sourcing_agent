from motor.motor_asyncio import AsyncIOMotorClient
import os

MONGO_URI_DEFAULT = 'mongodb+srv://jagadishpagolu1996:Ammananna%40143@cluster0.cwibfnc.mongodb.net/'


def get_client():
    """Get MongoDB client using MONGO_URI from env (set by config.load_secrets) or fallback."""
    uri = os.environ.get("MONGO_URI", MONGO_URI_DEFAULT)
    return AsyncIOMotorClient(uri)


client = get_client()

# Jobs database
jobs_db = client["IT_Jobs"]
job_collection_name = jobs_db['Scrapper_jobs_agent']

# Resume & evaluation database
resume_db = client["Kinnective_testing"]
resume_collection_name = resume_db['student_resume_details']
evaluation_collection = resume_db['job_candidate_evaluation']
tuned_resumes_collection = resume_db['tuned_resumes']

# Bench candidate resumes (uploaded by IT recruiters)
bench_resume_collection = jobs_db['bench_candidates_resume']

# Bench candidate evaluation results (replaces Kinnective_testing.job_candidate_evaluation)
bench_candidate_evaluation = jobs_db['bench_candidate_evaluation']








