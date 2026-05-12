from typing import Any, Dict, List, Optional, Annotated, TypedDict
from datetime import datetime
import operator

class RecruitmentState(TypedDict):
    run_id: str
    timestamp: str
    pending_jobs_id: List[str]
    current_job_index: int
    current_job_id: Optional[str]
    current_job: Optional[Dict[str, Any]]
    raw_jd: Optional[str]
    structured_jd: Optional[Dict[str, Any]]

    # candidates
    filtered_candidates: List[Dict[str, Any]]
    current_candidate_id: Optional[str]
    evaluations: Annotated[List[Dict[str, Any]], operator.add]

    # Output
    shortlisted_candidates: Annotated[List[Dict[str, Any]], operator.add]
    formatted_resume_data: Annotated[List[Dict[str, Any]], operator.add]
    tuned_candidates: Annotated[List[Dict[str, Any]], operator.add]
    summary: Optional[str]

    # docx output paths and their S3 URLs
    rendered_docx_paths: Annotated[List[str], operator.add]
    formatted_docx_urls: Annotated[List[Dict[str, Any]], operator.add]
    tuned_docx_urls: Annotated[List[Dict[str, Any]], operator.add]
    job_data_urls: Annotated[List[Dict[str, Any]], operator.add]

    # tracking
    processed_jobs: Annotated[List[str], operator.add]
    errors: Annotated[List[str], operator.add]
    email_sent: Optional[bool]