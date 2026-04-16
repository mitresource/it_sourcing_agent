import os
import json
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from firecrawl import Firecrawl
from pydantic import BaseModel, Field
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ====================== CONFIG ======================
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

SEARCH_URL = "https://www.dice.com/jobs?filters.postedDate=ONE&radius=30&q=java+developer&radiusUnit=mi"

DATA_FOLDER = Path("data/raw_jobs")
DATA_FOLDER.mkdir(parents=True, exist_ok=True)

EXTRACT_PROMPT = """
You are an expert IT recruiter data extractor.
From the Dice job page, extract the following fields exactly as per the schema.
Be precise, do not hallucinate.

- job_id: unique ID from the URL (last part after /jobs/)
- title: exact job title
- company: company name
- location: full location (city, state, remote info)
- posted_date: e.g. "2 days ago" or exact date
- employment_type: Contract, Full-time, etc.
- work_setting: Remote, Hybrid, Onsite
- distance_miles: number if mentioned, else null
- employer_type: Recruiter, Direct Hire, etc.
- salary: salary info or "Not disclosed"
- full_description: COMPLETE clean job description (remove ads, navigation, footer)
- key_skills: list of 8-15 most relevant technical skills (e.g. java, spring, aws)

Only return data that actually exists on the page.
"""

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

# ====================== MINI-AGENT 1: Scrape Search Results (List of Jobs) ======================
def agent_scrape_job_list(max_jobs: int = 10):
    app = Firecrawl(api_key=FIRECRAWL_API_KEY)
    
    result = app.scrape(
        url=SEARCH_URL,
        formats=[{
            "type": "json",
            "prompt": "Extract ONLY the list of visible job cards from this Dice search results page. Return an array called 'jobs' with: title, company, location, posted_date, job_url for each job."
        }]
    )
    
    # Extract the data (SDK usually returns dict with .json or ['json'])
    json_data = result.get("json") if isinstance(result, dict) else getattr(result, "json", {})
    jobs_list = json_data.get("jobs", [])[:max_jobs]
    
    print(f"✅ Agent 1: Found {len(jobs_list)} job links")
    return jobs_list

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
                    "prompt": EXTRACT_PROMPT,
                    "schema": JobPosting.model_json_schema()
                }]
            )
            
            structured = result.get("json") if isinstance(result, dict) else getattr(result, "json", {})
            
            # Add extra fields
            structured["job_url"] = job["job_url"]
            structured["job_id"] = job["job_url"].split("/")[-1].split("?")[0]
            structured["scraped_at"] = datetime.now().isoformat()
            
            all_jobs.append(structured)
            print(f"   Scraped: {structured.get('title', 'N/A')}")
            
        except Exception as e:
            print(f"   ⚠️ Error scraping {job.get('job_url')}: {e}")
    
    return all_jobs

# ====================== MINI-AGENT 3: Clean with OpenAI ======================
def agent_clean_jobs(raw_jobs):
    cleaned = []
    for job in raw_jobs:
        try:
            prompt = f"""
Clean and normalize this job data:
- Make key_skills lowercase, no duplicates
- Standardize employment_type and work_setting
- Return ONLY valid JSON that matches the JobPosting schema.

Raw data: {json.dumps(job, ensure_ascii=False)}
"""
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            cleaned_job = json.loads(response.choices[0].message.content)
            cleaned.append(cleaned_job)
        except Exception as e:
            print(f"   Cleaning error: {e}")
            cleaned.append(job)  # fallback to raw
    
    print(f"✅ Agent 3: Cleaned {len(cleaned)} jobs")
    return cleaned

# ====================== MINI-AGENT 4: Save ======================
def agent_save_jobs(jobs):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = DATA_FOLDER / f"dice_jobs_{timestamp}.json"
    
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)
    
    print(f"✅ Agent 4: Saved {len(jobs)} jobs → {filename}")
    return filename

# ====================== RUN PIPELINE ======================
if __name__ == "__main__":
    print("🚀 Starting Agentic Job Scraping Pipeline for Dice...\n")
    
    job_list = agent_scrape_job_list(max_jobs=10)   # Start with 10 jobs (safe for testing)
    
    if not job_list:
        print("❌ No jobs found. Please check your SEARCH_URL and try again.")
        exit()
    
    raw_jobs = agent_scrape_job_details(job_list)
    
    clean_jobs = agent_clean_jobs(raw_jobs)
    
    saved_file = agent_save_jobs(clean_jobs)
    
    print(f"\n🎉 Pipeline completed! Your structured jobs are saved here: {saved_file}")