"""
Resume Validation Utilities

Called after the formatter and tuner to verify output integrity before
anything is uploaded or sent. Both functions return (is_valid, errors).
"""

import re
from typing import Dict, Any, List, Tuple


def count_jobs_in_raw_text(raw_text: str) -> int:
    """
    Count work experience entries by detecting date-range patterns in raw text.
    Handles: "01/2024 to Current", "05/2023 to 12/2023", "08/2011 to 03/2016"
    """
    pattern = r'\b\d{2}/\d{4}\s+(?:to|–|—|-)\s+(?:\d{2}/\d{4}|Current|Present)\b'
    matches = re.findall(pattern, raw_text, re.IGNORECASE)
    return len(set(matches))


def validate_formatted_resume(structured: Dict[str, Any], raw_text: str) -> Tuple[bool, List[str]]:
    """
    Verify the formatter preserved all content from raw_text.
    Returns (is_valid, list_of_error_strings).
    """
    errors: List[str] = []

    raw_job_count = count_jobs_in_raw_text(raw_text)
    output_count = len(structured.get("work_experience", []))

    if raw_job_count > 0 and output_count < raw_job_count:
        errors.append(
            f"Missing work experience entries: raw resume has ~{raw_job_count} jobs "
            f"but output only contains {output_count}. "
            f"The most recent job is likely missing."
        )

    if output_count == 0:
        errors.append("No work_experience entries were produced at all.")

    profile = structured.get("candidate_profile_details", {})
    if not profile.get("name"):
        errors.append("Candidate name is missing from candidate_profile_details.")

    if not structured.get("education"):
        errors.append("Education section is empty or missing.")

    if not structured.get("profile_summary"):
        errors.append("profile_summary section is empty or missing.")

    if not structured.get("skills"):
        errors.append("skills section is empty or missing.")

    return len(errors) == 0, errors


def validate_tuned_resume(tuned: Dict[str, Any], formatted: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Verify the tuned resume preserves all content from the formatted resume.
    The tuned resume must be a strict superset — nothing removed, only additions allowed.
    Returns (is_valid, list_of_error_strings).
    """
    errors: List[str] = []

    fmt_count = len(formatted.get("work_experience", []))
    tuned_count = len(tuned.get("work_experience", []))

    if tuned_count < fmt_count:
        errors.append(
            f"Work experience entries reduced from {fmt_count} to {tuned_count}. "
            "Content was removed — tuning must only ADD content."
        )

    # All employer and client names from formatted must still appear in tuned
    def _employer_set(resume: Dict[str, Any]) -> set:
        names = set()
        for e in resume.get("work_experience", []):
            if e.get("employer_name", "").strip():
                names.add(e["employer_name"].strip().lower())
            if e.get("client_name", "").strip():
                names.add(e["client_name"].strip().lower())
        return names

    missing_names = _employer_set(formatted) - _employer_set(tuned)
    if missing_names:
        errors.append(
            f"Employer/client names from formatted resume are missing in tuned: {missing_names}"
        )

    # All original skills must still be present (tuning may only add, not remove)
    def _flat_skills(resume: Dict[str, Any]) -> set:
        return {
            s.strip().lower()
            for skills_list in resume.get("skills", {}).values()
            for s in (skills_list if isinstance(skills_list, list) else [])
            if s.strip()
        }

    removed_skills = _flat_skills(formatted) - _flat_skills(tuned)
    if removed_skills:
        errors.append(
            f"Original skills were removed in the tuned resume: {removed_skills}"
        )

    # Tuned must have at least as many summary bullets as formatted
    fmt_summary_len = len(formatted.get("profile_summary", []))
    tuned_summary_len = len(tuned.get("profile_summary", []))
    if tuned_summary_len < fmt_summary_len:
        errors.append(
            f"profile_summary shrank from {fmt_summary_len} to {tuned_summary_len} bullets. "
            "Summary content was removed."
        )

    return len(errors) == 0, errors


def build_formatter_retry_prompt(raw_resume: str, errors: List[str]) -> str:
    """Append correction instructions to the raw resume for a retry attempt."""
    error_lines = "\n".join(f"  - {e}" for e in errors)
    return (
        f"{raw_resume}\n\n"
        f"===== CORRECTION REQUIRED =====\n"
        f"Your previous output had these errors that MUST be fixed:\n"
        f"{error_lines}\n\n"
        f"ACTION: Count every job entry in the resume above (each starts with a date range "
        f"like '01/2024 to Current'). Your work_experience array must contain ALL of them "
        f"with no entries skipped, especially the most recent one."
    )
