import os
import json
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from firecrawl import Firecrawl
from pydantic import BaseModel, Field
from main import OpenAI
from dotenv import load_dotenv

load_dotenv()

FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

SEARCH_URL = "https://www.dice.com/jobs?filters.postedDate=ONE&filters.workplaceTypes=Remote&radius=30&q=generative+AI+developer&radiusUnit=mi"

DATA_FOLDER = Path("data/raw_jobs_test")
DATA_FOLDER.mkdir(parents=True, exist_ok=True)


# ====================== SCHEMA ======================
class JobPosting(BaseModel):
    job_id: str = Field(..., description="Unique Dice job ID from URL")
    title: str
    company: str
    location: str
    posted_date: str
    employment_type: str
    work_setting: str
    distance_miles: Optional[int] = None
    employer_type: str
    salary: Optional[str] = None
    full_description: str = Field(..., description="Full clean job description")
    key_skills: List[str] = Field(default_factory=list)
    job_url: str
    scraped_at: str

# ====================== MINI-AGENT 1: Scrape Search Results ======================
def agent_scrape_job_list(max_jobs: int = 10):
    app = Firecrawl(api_key=FIRECRAWL_API_KEY)
    
    result = app.scrape(
        url=SEARCH_URL,
        formats=[{
            "type": "json",
            "prompt": "Extract all visible job cards from this Dice search results page. Return a list called 'jobs' containing: title, company, location, posted_date, job_url for each job."
        }]
    )

    # Correct way to access structured JSON from Document object
    json_data = result.json if hasattr(result, "json") else {}
    jobs_list = json_data.get("jobs", []) if isinstance(json_data, dict) else []
    
    print(f"✅ Agent 1: Found {len(jobs_list)} job links")
    return jobs_list[:max_jobs]

# ====================== MINI-AGENT 2: Scrape Individual Job Details ======================
def agent_scrape_job_details(job_list):
    app = Firecrawl(api_key=FIRECRAWL_API_KEY)
    all_jobs = []
    
    for job in job_list:
        try:
            result = app.scrape(
                url=job["job_url"],
                formats=[{
                    "type": "json",
                    "prompt": """You are an expert IT recruiter data extractor. 
                    From the Dice job page, extract the fields exactly as per the schema.
                    Be precise, do not hallucinate.""",
                    "schema": JobPosting.model_json_schema()
                }]
            )
            
            # Correct access
            structured = result.json if hasattr(result, "json") else {}
            
            structured["job_url"] = job["job_url"]
            structured["job_id"] = job["job_url"].split("/")[-1].split("?")[0]
            structured["scraped_at"] = datetime.now().isoformat()
            
            all_jobs.append(structured)
            print(f"   Scraped: {structured.get('title', 'N/A')}")
            
        except Exception as e:
            print(f"   ⚠️ Error on {job.get('job_url')}: {e}")
    
    return all_jobs

# ====================== MINI-AGENT 3 & 4 remain mostly same (minor fixes) ======================
# ... (I kept your cleaning and save functions — they are fine)

# Run the pipeline (same as before)
if __name__ == "__main__":
    print("🚀 Starting Agentic Job Scraping Pipeline...\n")
    
    job_list = agent_scrape_job_list(max_jobs=8)   # start small
    
    if not job_list:
        print("❌ No jobs found on search page. Check the URL or Dice layout.")
        exit()
    
    raw_jobs = agent_scrape_job_details(job_list)
    # clean_jobs = agent_clean_jobs(raw_jobs)
    # saved_file = agent_save_jobs(clean_jobs)