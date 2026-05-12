from typing import Dict, Any
from collections import defaultdict
import logging
import os
import json
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("recruitment_agent")


def _truncate(text: str, max_lines: int = 6) -> str:
    if not text:
        return ""
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    return " ".join(lines[:max_lines])



async def send_summary_email(state: Dict[str, Any]) -> Dict[str, Any]:
    """Final node: Send shortlisted + tuned candidates summary via AWS SES."""

    shortlisted = state.get("shortlisted_candidates", [])
    tuned_candidates = state.get("tuned_candidates", [])
    all_evaluations = state.get("evaluations", [])
    run_id = state.get("run_id", "unknown")
    processed_jobs = state.get("processed_jobs", [])

    # Lookup: job_id → job data presigned URL
    job_data_url_lookup = {
        r["job_id"]: r["url"]
        for r in state.get("job_data_urls", [])
        if r.get("url")
    }

    sender = os.environ.get("SES_SENDER_EMAIL", "")
    recipients_raw = os.environ.get("SES_RECIPIENT_EMAILS", "")
    recipients = [e.strip() for e in recipients_raw.split(",") if e.strip()]
    region = os.environ.get("AWS_SES_REGION", os.environ.get("AWS_REGION", "us-west-2"))

    # Lookup: candidate_id -> tuned record
    tuned_by_candidate = {t["candidate_id"]: t for t in tuned_candidates}

    # DOCX S3 URL lookups (plain DOCX files uploaded after rendering)
    formatted_docx_lookup = {
        (r["job_id"], r["candidate_id"]): r["url"]
        for r in state.get("formatted_docx_urls", [])
    }
    tuned_docx_lookup = {
        (r["job_id"], r["candidate_id"]): r["url"]
        for r in state.get("tuned_docx_urls", [])
    }

    # Group shortlisted by job
    jobs_grouped = defaultdict(list)
    for candidate in shortlisted:
        key = (
            candidate.get("job_id", "unknown"),
            candidate.get("job_title", "Unknown Job"),
            candidate.get("posted_date", ""),
            candidate.get("job_url", ""),
        )
        jobs_grouped[key].append(candidate)

    unique_candidates_evaluated = len(set(e.get("candidate_id") for e in all_evaluations))
    total_shortlisted = len(set(c.get("candidate_id") for c in shortlisted))
    total_tuned = len(tuned_candidates)
    total_jobs = len(processed_jobs)

    html_body = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; color: #333; font-size: 14px; }}
            h2 {{ color: #2c3e50; }}
            h3 {{ color: #34495e; margin-top: 30px; border-bottom: 2px solid #3498db; padding-bottom: 5px; }}
            h4 {{ color: #2980b9; margin: 20px 0 6px 0; }}
            table {{ border-collapse: collapse; width: 100%; margin-top: 10px; }}
            th {{ background-color: #3498db; color: white; padding: 10px; text-align: left; font-size: 13px; }}
            td {{ padding: 9px 10px; border-bottom: 1px solid #ddd; vertical-align: top; font-size: 13px; }}
            tr:hover {{ background-color: #f9f9f9; }}
            .summary-box {{ background: #eaf2f8; padding: 15px; border-radius: 8px; margin-bottom: 20px; }}
            .score-high {{ color: #27ae60; font-weight: bold; }}
            .score-mid {{ color: #f39c12; font-weight: bold; }}
            .score-low {{ color: #e74c3c; font-weight: bold; }}
            .tuned-box {{ background: #f0fff4; border-left: 4px solid #27ae60; padding: 14px 18px; margin: 14px 0; border-radius: 4px; }}
            .score-compare {{ margin: 8px 0; }}
            .arrow {{ font-size: 18px; color: #27ae60; margin: 0 8px; }}
            .section-label {{ font-weight: bold; color: #555; margin-top: 10px; display: block; }}
            .divider {{ border: none; border-top: 1px dashed #ccc; margin: 14px 0; }}
            ul {{ margin: 6px 0; padding-left: 20px; }}
            li {{ margin: 3px 0; }}
        </style>
    </head>
    <body>
        <h2>IT Recruitment Agent - Daily Report</h2>
        <div class="summary-box">
            <strong>Run ID:</strong> {run_id}<br>
            <strong>Jobs Processed:</strong> {total_jobs}<br>
            <strong>Total Candidates Evaluated:</strong> {unique_candidates_evaluated}<br>
            <strong>Total Shortlisted:</strong> {total_shortlisted}<br>
            <strong>Resumes AI-Tuned:</strong> {total_tuned}
        </div>
    """

    if not shortlisted:
        html_body += "<p>No candidates were shortlisted in this run.</p>"
    else:
        for (job_id, job_title, posted_date, job_url), candidates in jobs_grouped.items():
            job_data_url = job_data_url_lookup.get(job_id, "")
            job_posting_link = (
                f'&nbsp;|&nbsp; <a href="{job_url}" target="_blank" '
                f'style="color:#e67e22;font-weight:bold;">🔗 View Job Posting</a>'
                if job_url else ""
            )
            job_pdf_link = (
                f'&nbsp;|&nbsp; <a href="{job_data_url}" target="_blank" '
                f'style="color:#8e44ad;font-weight:bold;">📋 Full Job Description</a>'
                if job_data_url else ""
            )
            html_body += f"""
            <h3>{job_title}</h3>
            <p><strong>Job ID:</strong> {job_id} &nbsp;|&nbsp;
               <strong>Posted:</strong> {posted_date} &nbsp;|&nbsp;
               <strong>Shortlisted:</strong> {len(candidates)}{job_posting_link}{job_pdf_link}</p>
            """

            # --- Summary table ---
            html_body += """
            <table>
                <tr>
                    <th>#</th>
                    <th>Candidate</th>
                    <th>Email</th>
                    <th>Before Score</th>
                    <th>After Score</th>
                    <th>Before Reason</th>
                    <th>After Reason</th>
                    <th>Formatted Resume</th>
                    <th>Tuned Resume</th>
                </tr>
            """
            sorted_candidates = sorted(candidates, key=lambda x: x.get("match_score", 0), reverse=True)
            for i, c in enumerate(sorted_candidates, 1):
                before = c.get("match_score", 0)
                cid = c.get("candidate_id", "")
                tuned = tuned_by_candidate.get(cid)
                after = tuned["after_score"] if tuned else "-"

                before_class = "score-high" if before >= 60 else "score-mid"
                after_class = ""
                if isinstance(after, int):
                    after_class = "score-high" if after >= 70 else ("score-mid" if after >= 50 else "score-low")

                before_reason = _truncate(c.get("reasoning", ""), max_lines=6)
                after_reason = _truncate(tuned.get("after_reasoning", "") if tuned else "", max_lines=6)

                fmt_url = formatted_docx_lookup.get((job_id, cid), "")
                tuned_url = tuned_docx_lookup.get((job_id, cid), "") if tuned else ""

                fmt_link = f'<a href="{fmt_url}" target="_blank" style="color:#2980b9;">📄 Download Formatted</a>' if fmt_url else "N/A"
                tuned_link = f'<a href="{tuned_url}" target="_blank" style="color:#27ae60;">📄 Download Tuned</a>' if tuned_url else "N/A"

                html_body += f"""
                <tr>
                    <td>{i}</td>
                    <td><strong>{c.get('candidate_name', 'N/A')}</strong></td>
                    <td>{c.get('candidate_email', 'N/A')}</td>
                    <td class="{before_class}">{before}</td>
                    <td class="{after_class}">{after}</td>
                    <td>{before_reason}</td>
                    <td>{after_reason}</td>
                    <td>{fmt_link}</td>
                    <td>{tuned_link}</td>
                </tr>
                """
            html_body += "</table>"

            # --- Compact tuned digest per candidate ---
            for c in sorted_candidates:
                cid = c.get("candidate_id", "")
                tuned = tuned_by_candidate.get(cid)
                if not tuned:
                    continue

                tuned_resume = tuned.get("tuned_resume", {})
                before = tuned["before_score"]
                after = tuned["after_score"]
                after_class = "score-high" if after >= 70 else ("score-mid" if after >= 50 else "score-low")

                # Single sentence from gap analysis
                full_gap = tuned.get("gap_analysis", "")
                gap_line = (full_gap.split(". ")[0] + ".") if full_gap else "—"

                # Missed skills from evaluation key_gaps
                key_gaps = c.get("key_gaps", [])
                missed_skills = ", ".join(key_gaps) if key_gaps else "—"

                # Experience — header lines only (no bullets)
                exp_lines = ""
                for exp in tuned_resume.get("experience", []):
                    employer = exp.get("employer_name", "") or exp.get("client_name", "")
                    client = exp.get("client_name", "") if exp.get("employer_name") else ""
                    timeframe = exp.get("time_frame", "") or exp.get("dates", "")
                    client_str = f"&nbsp;&nbsp;Client: {client}" if client else ""
                    exp_lines += f"<li>at {employer}{client_str}&nbsp;&nbsp;({timeframe})</li>"

                tuned_url = tuned_docx_lookup.get((job_id, cid), "")
                tuned_link = f'<a href="{tuned_url}" target="_blank" style="color:#27ae60;font-weight:bold;">📄 Download Tuned Resume</a>' if tuned_url else "N/A"

                html_body += f"""
                <div class="tuned-box">
                    <strong>{tuned['candidate_name']}</strong> &nbsp;|&nbsp;
                    Score: <span class="score-mid"><b>{before}</b></span>
                    <span class="arrow">&#8594;</span>
                    <span class="{after_class}"><b>{after}</b></span>
                    &nbsp;|&nbsp; {tuned_link}
                    <br><br>
                    <span class="section-label">Gap</span>
                    <p style="margin:4px 0;">{gap_line}</p>
                    <span class="section-label">Missed Skills</span>
                    <p style="margin:4px 0;">{missed_skills}</p>
                    <span class="section-label">Experience</span>
                    <ul style="margin:4px 0;">{exp_lines}</ul>
                </div>
                """

    html_body += """
        <br>
        <p style="color: #999; font-size: 12px;">
            This is an automated report from the IT Sourcing Agent.<br>
            Tuned resumes are AI-generated to better match the JD — review before sending to clients.
        </p>
    </body>
    </html>
    """

    subject = f"IT Recruitment Report - {total_shortlisted} Shortlisted | {total_tuned} Resumes AI-Tuned"

    cache_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "last_run_email_cache.json")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"subject": subject, "html_body": html_body, "run_id": run_id}, f, ensure_ascii=False)
    logger.info(f"Email cache saved to {cache_path}")

    try:
        ses_client = boto3.client("ses", region_name=region)
        ses_client.send_email(
            Source=sender,
            Destination={"ToAddresses": recipients},
            Message={
                "Subject": {
                    "Data": subject,
                    "Charset": "UTF-8",
                },
                "Body": {
                    "Html": {
                        "Data": html_body,
                        "Charset": "UTF-8",
                    }
                },
            },
        )
        logger.info(f"Email sent to {', '.join(recipients)}")
        return {"email_sent": True}

    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_msg = e.response['Error']['Message']
        logger.error(f"SES email failed [{error_code}]: {error_msg}")
        logger.error(f"  Sender: {sender}")
        logger.error(f"  Recipients: {recipients}")
        logger.error(f"  Region: {region}")
        return {"email_sent": False}
    except Exception as e:
        logger.error(f"Email sending failed: {type(e).__name__}: {e}")
        return {"email_sent": False}
