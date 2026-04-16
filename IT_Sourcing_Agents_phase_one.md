# IT Sourcing Agent - Phase One Architecture

## Overview

An autonomous multi-agent recruitment pipeline built with **LangGraph** that matches bench candidates against open job postings using **GPT-4o-mini**, stores results in **MongoDB**, and sends daily email reports via **AWS SES**.

Triggered daily at **5:00 PM IST** via cron.

---

## High-Level Architecture

```
                          +---------------------------+
                          |      AWS Cloud Layer      |
                          |                           |
                          |  +---------------------+  |
                          |  | Secrets Manager      |  |
                          |  | (ap-south-1)         |  |
                          |  | - OPENAI_API_KEY     |  |
                          |  | - MONGO_URI          |  |
                          |  +---------------------+  |
                          |                           |
                          |  +---------------------+  |
                          |  | SES Email Service    |  |
                          |  | (ap-south-1)         |  |
                          |  +---------------------+  |
                          +------------+--------------+
                                       |
                          +------------v--------------+
                          |       runner.py            |
                          |   (Cron Entry Point)       |
                          |   5:00 PM IST daily        |
                          |   cron: 30 11 * * *        |
                          +------------+--------------+
                                       |
                     +-----------------v------------------+
                     |        LangGraph State Machine      |
                     |        (recruitment_app)             |
                     |                                      |
                     |  +--------------------------------+  |
                     |  | Node 1: fetch_pending_jobs     |  |
                     |  +---------------+----------------+  |
                     |                  |                    |
                     |  +---------------v----------------+  |
                     |  | Node 2: prepare_job_data       |<---+
                     |  +---------------+----------------+  | |
                     |                  |                    | |
                     |  +---------------v----------------+  | |
                     |  | Node 3: evaluate_candidates    |  | |
                     |  +---------------+----------------+  | |
                     |                  |                    | |
                     |  +---------------v----------------+  | |
                     |  | Node 4: save_results_to_files  |  | |
                     |  +---------------+----------------+  | |
                     |                  |                    | |
                     |          +-------v--------+          | |
                     |          | more jobs?     |----------+ |
                     |          +-------+--------+  YES       |
                     |                  | NO                   |
                     |  +---------------v----------------+    |
                     |  | Node 5: send_summary_email     |    |
                     |  +--------------------------------+    |
                     +-----------------------------------------+
                                       |
              +------------------------+------------------------+
              |                        |                        |
     +--------v--------+    +---------v---------+    +---------v---------+
     | MongoDB Atlas    |    | Local File System |    | Email Recipients  |
     | - IT_Jobs        |    | agent_resume_     |    | via AWS SES       |
     | - Kinnective_    |    | results/          |    |                   |
     |   testing        |    |                   |    |                   |
     +------------------+    +-------------------+    +-------------------+
```

---

## Startup Sequence

```
runner.py
  |
  +-- load_dotenv()                 Load .env (SES emails config)
  |
  +-- load_secrets()                Fetch from AWS Secrets Manager
  |     |                           Secret: "IT_SOURCING_AGENT" (ap-south-1)
  |     +-- Sets OPENAI_API_KEY     Used by LangChain ChatOpenAI
  |     +-- Sets MONGO_URI          Used by Motor (MongoDB client)
  |     +-- Falls back to .env      If AWS is unreachable (local dev)
  |
  +-- import recruitment_app        Compiles the LangGraph workflow
  |     +-- db.py reads MONGO_URI   Connects to MongoDB Atlas
  |     +-- ChatOpenAI reads key    Initializes GPT-4o-mini
  |
  +-- ainvoke(initial_state={})     Starts the pipeline
  |
  +-- Log results + exit code       0=success, 1=failure
```

---

## Graph Nodes (Detailed)

### Node 1: `fetch_pending_jobs_from_db`
**File:** `agents/nodes/fetch_jobs.py`

| Aspect | Detail |
|--------|--------|
| Input | Empty initial state |
| Database | `IT_Jobs.Scrapper_jobs_agent` |
| Query | `processed: false` OR `processed` field doesn't exist |
| Sort | `scraped_at` descending (newest first) |
| Output | `run_id`, `timestamp`, `pending_jobs_id[]`, `current_job_index: 0` |

---

### Node 2: `prepare_job_data`
**File:** `agents/nodes/parse_jobs.py`

| Aspect | Detail |
|--------|--------|
| Input | `pending_jobs_id[current_job_index]` |
| Action | Fetches full job document by ObjectId, structures it |
| Output | `structured_jd`, `current_job_id`, `raw_jd` |

**Field Mapping (IT_Jobs schema):**

```
Job Document                    structured_jd
-----------                     -------------
title                    -->    job_title
company                  -->    company
location                 -->    location
job_type                 -->    job_type
skills[]                 -->    required_skills[]
responsibilities[]       -->    responsibilities[]
qualifications[]         -->    qualifications[]
full_summary / raw_text  -->    description
posted_date              -->    posted_date
experience               -->    experience
salary                   -->    salary
apply_link               -->    apply_link
```

---

### Node 3: `evaluate_candidate_matches`
**File:** `agents/nodes/evaluate_candidates_matches.py`

| Aspect | Detail |
|--------|--------|
| LLM | GPT-4o-mini (temperature=0) via LangChain |
| Candidates | `Kinnective_testing.student_resume_details` where `is_bench: "yes"` |
| Prompt | Expert IT recruiter matching job vs resume |
| Output per candidate | `match_score` (0-100), `shortlisted` (bool), `reasoning`, `key_strengths[]`, `key_gaps[]` |
| Shortlist threshold | Score >= 40 |
| DB Write | Saves all evaluations to `Kinnective_testing.job_candidate_evaluation` |

**Evaluation Record Schema:**
```json
{
  "job_id":            "MongoDB ObjectId string",
  "job_title":         "Python Developer",
  "posted_date":       "22 hours ago",
  "candidate_id":      "MongoDB ObjectId string",
  "resume_id":         "same as candidate_id",
  "candidate_name":    "John Doe",
  "candidate_email":   "john@example.com",
  "match_score":       75,
  "shortlisted":       true,
  "reasoning":         "Strong Python skills...",
  "key_strengths":     ["Python", "AWS", "REST APIs"],
  "key_gaps":          ["No React experience"],
  "evaluated_at":      "2026-04-16T17:00:00"
}
```

---

### Node 4: `save_results_to_files`
**File:** `agents/nodes/save_results.py`

| Aspect | Detail |
|--------|--------|
| Filters | Only evaluations for `current_job_id` (not accumulated from prior jobs) |
| Output dir | `agent_resume_results/<JobTitle>_<JobId>/` |
| Per candidate | `<CandidateName>.json` with full evaluation |
| Summary | `_summary.json` with all candidates ranked by score |
| DB Update | Marks job as `processed: true` in `IT_Jobs.Scrapper_jobs_agent` |
| Loop control | Increments `current_job_index` for next iteration |

**Output folder structure:**
```
agent_resume_results/
  +-- Python_Developer_69dccd9b/
  |     +-- _summary.json
  |     +-- Ashok_Palla.json
  |     +-- Naveen_Anantha.json
  |     +-- Ram_Kumar.json
  |
  +-- Marketing_Consultant_69dcd22e/
        +-- _summary.json
        +-- Ashok_Palla.json
        +-- ...
```

---

### Conditional: `has_more_jobs`
**File:** `agents/graphs/graph.py`

```
if current_job_index < len(pending_jobs_id):
    --> route back to Node 2 (prepare_job_data)
else:
    --> route to Node 5 (send_summary_email)
```

---

### Node 5: `send_summary_email`
**File:** `agents/nodes/send_email.py`

| Aspect | Detail |
|--------|--------|
| Service | AWS SES (ap-south-1) |
| Auth | AWS CLI credentials (~/.aws/credentials) |
| From | `jpagolu@mitresource.com` |
| To | Comma-separated list from `SES_RECIPIENT_EMAILS` env var |
| Format | HTML email with styled tables |

**Email Structure:**
```
Subject: IT Recruitment Report - X Shortlisted across Y Jobs

Body:
+------------------------------------------+
| IT Recruitment Agent - Daily Report      |
|                                          |
| Run ID: abc-123                          |
| Jobs Processed: 10                       |
| Total Candidates Evaluated: 50           |
| Total Shortlisted: 7                     |
+------------------------------------------+

--- Python Developer ---
Job ID: 69dccd9b | Posted: 22 hours ago | Shortlisted: 2

| # | Name       | Email           | Resume ID | Score | Reasoning  |
|---|------------|-----------------|-----------|-------|------------|
| 1 | John Doe   | john@email.com  | 681c363.. | 85    | Strong ... |
| 2 | Jane Smith | jane@email.com  | 682a481.. | 62    | Good ...   |

--- Data Engineer ---
...
```

**Current Recipients:**
- akommu@mitresources.com
- bardhalapudi@mitresource.com
- jagadishpagolu1996@gmail.com

---

## State Management

The pipeline uses **LangGraph StateGraph** with a shared `RecruitmentState` TypedDict. Fields marked with `operator.add` accumulate across the job loop.

```
RecruitmentState
  |-- run_id: str                                    (set once)
  |-- timestamp: str                                 (set once)
  |-- pending_jobs_id: List[str]                     (set once)
  |-- current_job_index: int                         (incremented per job)
  |-- current_job_id: Optional[str]                  (overwritten per job)
  |-- current_job: Optional[Dict]                    (overwritten per job)
  |-- raw_jd: Optional[str]                          (overwritten per job)
  |-- structured_jd: Optional[Dict]                  (overwritten per job)
  |-- evaluations: List[Dict]               [+add]   (accumulated)
  |-- shortlisted_candidates: List[Dict]    [+add]   (accumulated)
  |-- processed_jobs: List[str]             [+add]   (accumulated)
  |-- errors: List[str]                     [+add]   (accumulated)
  |-- email_sent: Optional[bool]                     (set at end)
```

---

## Database Architecture

```
MongoDB Atlas Cluster
  |
  +-- IT_Jobs (database)
  |     |
  |     +-- Scrapper_jobs_agent (collection)
  |           - 31 job documents (scraped from Dice.com)
  |           - Fields: title, company, location, skills[], 
  |             responsibilities[], posted_date, processed, ...
  |
  +-- Kinnective_testing (database)
        |
        +-- student_resume_details (collection)
        |     - Bench candidate resumes
        |     - Fields: name, email, skills[], experience[],
        |       education[], certifications[], is_bench, ...
        |
        +-- job_candidate_evaluation (collection)
              - LLM evaluation results (written by Node 3)
              - Fields: job_id, candidate_id, match_score,
                shortlisted, reasoning, key_strengths[], ...
```

---

## AWS Services Used

| Service | Region | Purpose |
|---------|--------|---------|
| **Secrets Manager** | ap-south-1 | Stores `OPENAI_API_KEY` and `MONGO_URI` securely |
| **SES** | ap-south-1 | Sends daily recruitment report emails |
| **EC2 / ECS** (cron host) | -- | Runs `runner.py` on schedule |

**Secret Name:** `IT_SOURCING_AGENT`
**Secret Keys:** `OPENAI_API_KEY`, `MONGO_URI`

---

## Project Structure

```
IT_Sourcing_agent/
  |-- runner.py                          Entry point (cron target)
  |-- requirements.txt                   Python dependencies
  |-- .env                               SES email config
  |-- .env.example                       Template for new setups
  |
  |-- agents/
  |     |-- config.py                    AWS Secrets Manager loader
  |     |-- db.py                        MongoDB connection setup
  |     |-- __init__.py
  |     |
  |     |-- nodes/
  |     |     |-- state.py               RecruitmentState TypedDict
  |     |     |-- fetch_jobs.py          Node 1: Fetch pending jobs
  |     |     |-- parse_jobs.py          Node 2: Structure job data
  |     |     |-- evaluate_candidates_matches.py  Node 3: LLM matching
  |     |     |-- save_results.py        Node 4: Save + mark processed
  |     |     |-- send_email.py          Node 5: SES email report
  |     |     |-- __init__.py
  |     |
  |     |-- graphs/
  |           |-- graph.py               LangGraph workflow definition
  |           |-- __init__.py
  |
  |-- agent_resume_results/              Output JSONs per job run
  |-- logs/                              Daily log files (run_YYYY-MM-DD.log)
  |-- itagentenv/                        Python virtual environment
```

---

## Cron Configuration

| Setting | Value |
|---------|-------|
| Schedule | `30 11 * * *` (11:30 UTC = 5:00 PM IST) |
| Command | `cd /path/to/IT_Sourcing_agent && PYTHONIOENCODING=utf-8 python runner.py` |
| Exit codes | `0` = success, `1` = failure |
| Logs | `logs/run_YYYY-MM-DD.log` (auto-created per day) |

---

## Logging

Every pipeline run writes to both **console** and **file**:

```
logs/
  +-- run_2026-04-16.log
  +-- run_2026-04-17.log
  +-- ...
```

**Log format:** `2026-04-16 17:00:05 [INFO] Starting autonomous recruitment process...`

**What gets logged:**
- Secrets Manager load status
- Number of pending jobs found
- Per-job: title, skills count, candidate scores
- Evaluation results and shortlist counts
- Email send success/failure
- Full stack traces on any errors (API key invalid, quota exceeded, DB timeout, etc.)

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Orchestration | LangGraph (StateGraph) |
| LLM | OpenAI GPT-4o-mini |
| LLM Framework | LangChain |
| Database | MongoDB Atlas (Motor async driver) |
| Secrets | AWS Secrets Manager |
| Email | AWS SES |
| Language | Python 3.11+ (asyncio) |
| Scheduling | Cron (managed by cloud team) |

---

## Data Flow Summary

```
[Dice.com Jobs]                    [Bench Candidates]
      |                                   |
      v                                   v
 IT_Jobs.Scrapper_jobs_agent    Kinnective_testing.student_resume_details
      |                                   |
      +-----------------------------------+
                      |
                      v
            GPT-4o-mini Evaluation
            (score 0-100, shortlist >= 40)
                      |
          +-----------+-----------+
          |           |           |
          v           v           v
   MongoDB eval   JSON files    SES Email
   collection     per job       to team
```
