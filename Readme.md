# Recruiter Agent - Bench Resume Analyzer
 
An AI-powered recruitment matching system that automatically scores bench candidates against recent job descriptions. Built with **FastAPI**, **LangGraph**, **OpenAI Embeddings**, and **MongoDB**.
 
---
 
## What It Does
 
1. Fetches **jobs posted in the last 3 days** from the `scrapped_jobs` collection
2. Fetches **bench candidates** (`is_bench: "yes"`) from the `student_resume_details` collection
3. Generates **OpenAI embeddings** for both job descriptions and resume text
4. Computes **cosine similarity** scores for every job-resume pair
5. Persists all results to the `analyzed_bench_results` collection in MongoDB
6. Returns a JSON summary via the API
 
---
 
## Architecture Overview
 
```
POST /recruit
     |
     v
+------------------+     +------------------+     +------------------+
|  Scraper Node    | --> |  Bench Node      | --> |  Scorer Node     |
|                  |     |                  |     |                  |
| - Query MongoDB  |     | - Query MongoDB  |     | - Cosine similarity|
|   scrapped_jobs  |     |   student_resume |     |   for every      |
|   (last 3 days)  |     |   _details       |     |   job x resume   |
| - Generate       |     |   (is_bench=yes) |     | - Save results   |
|   embeddings     |     | - Flatten skills |     |   to MongoDB     |
|   for job desc   |     | - Stringify exp  |     |   analyzed_bench |
|                  |     | - Generate       |     |   _results       |
|                  |     |   embeddings     |     |                  |
+------------------+     +------------------+     +------------------+
```
 
**Orchestration:** LangGraph StateGraph with a linear pipeline: `scraper -> bench -> scorer -> END`
 
---
 
## Project Structure
 
```
Recruiter_Agent/
|-- app/
|   |-- main.py                  # FastAPI app & /recruit endpoint
|   |-- agents/                  # LangGraph workflow nodes
|   |   |-- graph.py             # Graph definition & compilation
|   |   |-- state.py             # GraphState & MatchResult types
|   |   |-- scrapper.py          # Node 1: Fetch jobs + embeddings
|   |   |-- bench_node.py        # Node 2: Fetch resumes + embeddings
|   |   |-- scorer_node.py       # Node 3: Score pairs + persist
|   |   |-- tuner_node.py        # (Inactive) GPT resume rewriter
|   |-- core/                    # Infrastructure
|   |   |-- config.py            # Settings via pydantic-settings
|   |   |-- database.py          # MongoDB Motor async client
|   |-- schemas/                 # Pydantic data models
|   |   |-- job.py               # Job model (maps scrapped_jobs)
|   |   |-- resume.py            # Resume model (maps student_resume_details)
|   |-- services/                # External service integrations
|   |   |-- mongo_src.py         # MongoDB queries, data normalization
|   |   |-- embedding_src.py     # OpenAI embedding generation
|   |-- utlis/                   # Utilities
|       |-- helper.py            # Cosine similarity function
|-- .env                         # Environment variables
|-- requirement.txt              # Python dependencies
```
 
---
 
## File-by-File Breakdown
 
### `app/main.py` - API Entry Point
 
- Creates the **FastAPI** application with a lifespan handler for DB cleanup
- **`POST /recruit`** endpoint:
  1. Clears previous results from `analyzed_bench_results`
  2. Invokes the LangGraph pipeline
  3. Returns summary stats (total jobs, resumes, pairs, matched, rejected) + full results
- **`GET /health`** - Simple health check
 
### `app/core/config.py` - Configuration
 
Loads settings from `.env` using `pydantic-settings`:
 
| Setting | Default | Purpose |
|---------|---------|---------|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `recruiter` | Database name |
| `OPENAI_API_KEY` | `""` | OpenAI API key |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model |
| `OPENAI_CHAT_MODEL` | `gpt-4.1-nano` | Chat model (for tuner) |
| `SCORE_THRESHOLD` | `0.5` | Minimum score to mark as "Matched" |
| `JOB_LOOKBACK_DAYS` | `3` | How many days back to fetch jobs |
 
### `app/core/database.py` - MongoDB Client
 
- Async MongoDB client using **Motor** (`AsyncIOMotorClient`)
- Singleton pattern - one global client reused across requests
- `get_database()` returns the configured database instance
- `close_database()` cleans up on app shutdown
 
### `app/schemas/job.py` - Job Data Model
 
Maps to the `scrapped_jobs` MongoDB collection:
 
| Model Field | DB Field | Type |
|-------------|----------|------|
| `id` | `_id` | str |
| `title` | `position_title` | str |
| `company` | `company` | str |
| `description` | `description` | str |
| `required_skills` | `required_skills` | str |
| `application_posted` | `application_posted` | str (YYYY-MM-DD) |
| `embedding` | (generated) | list[float] |
 
Uses `extra = "ignore"` to silently drop unknown fields from the DB document.
 
### `app/schemas/resume.py` - Resume Data Model
 
Maps to the `student_resume_details` MongoDB collection:
 
| Model Field | DB Field | Type |
|-------------|----------|------|
| `id` | `_id` | str |
| `candidate_name` | `name` | str |
| `email` | `email` | str |
| `summary` | `summary` | str |
| `skills` | `skills` (flattened) | list[str] |
| `experience` | `experience` (stringified) | str |
| `is_bench` | `is_bench` | str/bool |
| `embedding` | (generated) | list[float] |
 
### `app/services/mongo_src.py` - MongoDB Operations
 
The core data access layer. Contains:
 
- **`fetch_recent_jobs()`** - Queries `scrapped_jobs` where `application_posted >= (today - 3 days)`. Uses string comparison on YYYY-MM-DD format dates.
 
- **`fetch_bench_resumes()`** - Queries `student_resume_details` where `is_bench` is "yes" (case-insensitive). Also normalizes the nested data:
  - **Skills flattening**: `[{category: "Technical", skills: [{name: "Python"}, ...]}]` becomes `["Python", ...]`
  - **Experience stringifying**: `[{role: "Dev", organization: "Cognizant", description: [...]}]` becomes `"Dev at Cognizant: desc1. desc2"`
 
- **`save_analyzed_results(results)`** - Inserts match results into `analyzed_bench_results` via `insert_many`
 
- **`clear_analyzed_results()`** - Deletes all documents from `analyzed_bench_results` (called before each run)
 
- **`_flatten_skills(raw_skills)`** - Helper to extract skill names from nested category/skill objects
 
- **`_stringify_experience(raw_experience)`** - Helper to convert experience objects into readable text
 
### `app/services/embedding_src.py` - OpenAI Embeddings
 
- Singleton `AsyncOpenAI` client
- **`get_embeddings(texts)`** - Batch-generates embedding vectors for a list of texts using `text-embedding-3-small`
- Returns list of float vectors (one per input text)
 
### `app/agents/state.py` - State Types
 
Defines the data flowing through the LangGraph pipeline:
 
- **`MatchResult`** - One job-resume pair with all context:
  - Job info: `job_id`, `job_title`, `job_company`, `job_description`, `required_skills`
  - Resume info: `resume_id`, `candidate_name`, `candidate_email`, `candidate_skills`, `candidate_summary`
  - Score: `match_score` (0.0 - 1.0), `status` ("Matched"/"Rejected"), `analyzed_at`
 
- **`GraphState`** - The shared state passed between nodes:
  - `jobs: list[Job]`
  - `resumes: list[Resume]`
  - `matches: list[MatchResult]`
 
### `app/agents/graph.py` - Workflow Definition
 
Builds and compiles the LangGraph state machine:
 
```
scraper --> bench --> scorer --> END
```
 
- Uses `StateGraph(GraphState)` for typed state management
- Each node reads/writes specific keys in the shared state
- The compiled graph is created once at module import time
 
### `app/agents/scrapper.py` - Node 1: Job Fetcher
 
1. Calls `fetch_recent_jobs()` to get jobs from last 3 days
2. Parses raw documents into `Job` Pydantic models
3. Extracts job descriptions and generates OpenAI embeddings
4. Returns `{"jobs": [Job, ...]}` to the graph state
 
### `app/agents/bench_node.py` - Node 2: Resume Fetcher
 
1. Calls `fetch_bench_resumes()` to get bench candidates
2. Parses raw documents into `Resume` Pydantic models (skills already flattened, experience already stringified)
3. Composes embedding text: `"{summary} {skills joined} {experience}"`
4. Generates OpenAI embeddings for each resume
5. Returns `{"resumes": [Resume, ...]}` to the graph state
 
### `app/agents/scorer_node.py` - Node 3: Matcher & Persister
 
1. Iterates: **for each job -> for each resume**
2. Computes **cosine similarity** between job and resume embeddings
3. Marks as `"Matched"` if score >= `SCORE_THRESHOLD` (0.5), else `"Rejected"`
4. Builds full `MatchResult` with all job/resume context
5. **Persists** all results to `analyzed_bench_results` collection via `save_analyzed_results()`
6. Returns `{"matches": [MatchResult, ...]}` to the graph state
 
### `app/agents/tuner_node.py` - (Inactive) Resume Tuner
 
Not part of the current workflow. Previously used GPT-4o to rewrite matched resumes to better align with job descriptions. Kept for potential future use.
 
### `app/utlis/helper.py` - Vector Math
 
- **`cosine_similarity(vec_a, vec_b)`** - Computes cosine similarity between two vectors using NumPy
- Formula: `dot(a, b) / (||a|| * ||b||)`
- Returns a float between 0.0 and 1.0
 
---
 
## MongoDB Collections
 
| Collection | Database | Purpose |
|-----------|----------|---------|
| `scrapped_jobs` | Kinnective_testing | Source: scraped job postings |
| `student_resume_details` | Kinnective_testing | Source: candidate resumes |
| `analyzed_bench_results` | Kinnective_testing | Output: job-resume match scores |
 
### `analyzed_bench_results` Document Schema
 
```json
{
  "job_id": "69cb5f867ba3158ef4bc42a5",
  "job_title": "Python Developer",
  "job_company": "ALTOS TECHNOLOGIES",
  "job_description": "Support the development...",
  "required_skills": "Python programming, Software development...",
  "resume_id": "681456f8767bd54e6f49b290",
  "candidate_name": "Ashok Palla",
  "candidate_email": "ashok.palla93@gmail.com",
  "candidate_skills": ["Python", "Communication"],
  "candidate_summary": "Industry experienced IT professional...",
  "match_score": 0.8234,
  "status": "Matched",
  "analyzed_at": "2026-04-10T12:00:00+00:00"
}
```
 
---
 
## Data Flow
 
```
scrapped_jobs (MongoDB)          student_resume_details (MongoDB)
       |                                    |
       v                                    v
  fetch_recent_jobs()               fetch_bench_resumes()
  (last 3 days filter)             (is_bench="yes" filter)
       |                           (flatten skills, stringify exp)
       v                                    v
  Generate embeddings               Generate embeddings
  (job descriptions)               (summary + skills + experience)
       |                                    |
       +------------------------------------+
                        |
                        v
              Scorer: for each job x resume
              compute cosine_similarity(job_emb, resume_emb)
              classify: Matched (>= 0.5) or Rejected
                        |
                        v
              Save to analyzed_bench_results (MongoDB)
                        |
                        v
              Return JSON response via API
```
 
---
 
## How to Run
 
```bash
# Install dependencies
pip install -r requirement.txt
 
# Set environment variables in .env
# OPENAI_API_KEY=sk-...
# MONGO_URI=mongodb+srv://...
# MONGO_DB=Kinnective_testing
 
# Start the server
uvicorn app.main:app --reload
 
# Trigger the workflow
curl -X POST http://localhost:8000/recruit
```
 
---
 
## API Reference
 
### `POST /recruit`
 
Runs the full bench analysis workflow.
 
**Response:**
```json
{
  "total_jobs": 4,
  "total_resumes": 5,
  "total_pairs": 20,
  "matched": 12,
  "rejected": 8,
  "results": [
    {
      "job_id": "...",
      "job_title": "Python Developer",
      "job_company": "ALTOS TECHNOLOGIES",
      "job_description": "...",
      "required_skills": "Python, Django...",
      "resume_id": "...",
      "candidate_name": "Ashok Palla",
      "candidate_email": "ashok.palla93@gmail.com",
      "candidate_skills": ["Python", "Django"],
      "candidate_summary": "...",
      "match_score": 0.8234,
      "status": "Matched",
      "analyzed_at": "2026-04-10T12:00:00+00:00"
    }
  ]
}
```
 
### `GET /health`
 
Returns `{"status": "ok"}`.
 
---
 
## Dependencies
 
| Package | Purpose |
|---------|---------|
| `fastapi` | Web framework & API |
| `uvicorn` | ASGI server |
| `motor` | Async MongoDB driver |
| `pymongo` | MongoDB driver (motor dependency) |
| `openai` | OpenAI API (embeddings) |
| `langgraph` | Agent workflow orchestration |
| `langchain-core` | LangGraph dependency |
| `numpy` | Vector math (cosine similarity) |
| `pydantic` | Data validation & schemas |
| `pydantic-settings` | Settings from .env |
| `python-dotenv` | .env file loading |