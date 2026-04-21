from typing import Dict, Any
from collections import defaultdict
import os
import boto3
from botocore.exceptions import ClientError


def _truncate(text: str, max_lines: int = 6) -> str:
    """Truncate text to max_lines lines."""
    if not text:
        return ""
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    return " ".join(lines[:max_lines])


async def send_summary_email(state: Dict[str, Any]) -> Dict[str, Any]:
    """Final node: Send shortlisted + tuned candidates summary via AWS SES."""

    shortlisted = state.get("shortlisted_candidates", [])
    tuned_candidates = state.get("tuned_candidates", [])
    formatted_resume_data = state.get("formatted_resume_data", [])
    all_evaluations = state.get("evaluations", [])
    run_id = state.get("run_id", "unknown")
    processed_jobs = state.get("processed_jobs", [])

    sender = os.environ.get("SES_SENDER_EMAIL", "jpagolu@mitresource.com")
    recipients_raw = os.environ.get("SES_RECIPIENT_EMAILS", "akommu@mitresources.com")
    recipients = [e.strip() for e in recipients_raw.split(",") if e.strip()]
    region = os.environ.get("AWS_REGION", "ap-south-1")

    # Lookup: candidate_id -> tuned record
    tuned_by_candidate = {t["candidate_id"]: t for t in tuned_candidates}

    # Lookup: (job_id, candidate_id) -> formatted presigned URL
    formatted_url_lookup = {
        (r["job_id"], r["candidate_id"]): r.get("formatted_presigned_url", "")
        for r in formatted_resume_data
    }

    # Group shortlisted by job
    jobs_grouped = defaultdict(list)
    for candidate in shortlisted:
        key = (
            candidate.get("job_id", "unknown"),
            candidate.get("job_title", "Unknown Job"),
            candidate.get("posted_date", ""),
        )
        jobs_grouped[key].append(candidate)

    total_evaluated = len(all_evaluations)
    total_shortlisted = len(shortlisted)
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
            <strong>Total Candidates Evaluated:</strong> {total_evaluated}<br>
            <strong>Total Shortlisted:</strong> {total_shortlisted}<br>
            <strong>Resumes AI-Tuned:</strong> {total_tuned}
        </div>
    """

    if not shortlisted:
        html_body += "<p>No candidates were shortlisted in this run.</p>"
    else:
        for (job_id, job_title, posted_date), candidates in jobs_grouped.items():
            html_body += f"""
            <h3>{job_title}</h3>
            <p><strong>Job ID:</strong> {job_id} &nbsp;|&nbsp;
               <strong>Posted:</strong> {posted_date} &nbsp;|&nbsp;
               <strong>Shortlisted:</strong> {len(candidates)}</p>
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

                fmt_url = formatted_url_lookup.get((job_id, cid), "")
                tuned_url = tuned.get("tuned_presigned_url", "") if tuned else ""

                fmt_link = f'<a href="{fmt_url}" target="_blank" style="color:#2980b9;">View</a>' if fmt_url else "N/A"
                tuned_link = f'<a href="{tuned_url}" target="_blank" style="color:#27ae60;">View</a>' if tuned_url else "N/A"

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

            # --- Tuned resume detail per candidate ---
            for c in sorted_candidates:
                cid = c.get("candidate_id", "")
                tuned = tuned_by_candidate.get(cid)
                if not tuned:
                    continue

                tuned_resume = tuned.get("tuned_resume", {})
                before = tuned["before_score"]
                after = tuned["after_score"]

                html_body += f"""
                <h4>AI-Tuned Resume: {tuned['candidate_name']}</h4>
                <div class="score-compare">
                    <span class="score-mid"><strong>Before:</strong> {before}/100</span>
                    <span class="arrow">&#8594;</span>
                    <span class="score-high"><strong>After:</strong> {after}/100</span>
                </div>

                <div class="tuned-box">
                    <span class="section-label">Professional Summary</span>
                    <p>{tuned_resume.get('summary', '')}</p>

                    <hr class="divider">
                    <span class="section-label">Skills</span>
                    <p>{', '.join(tuned_resume.get('skills', []))}</p>

                    <hr class="divider">
                    <span class="section-label">Experience</span>
                """

                for exp in tuned_resume.get("experience", []):
                    bullets_html = "".join(f"<li>{b}</li>" for b in exp.get("bullets", []))
                    html_body += f"""
                    <p><strong>{exp.get('title', '')} at {exp.get('company', '')}</strong>
                       &nbsp;({exp.get('dates', '')})</p>
                    <ul>{bullets_html}</ul>
                    """

                edu_list = tuned_resume.get("education", [])
                if edu_list:
                    edu_items = "".join(
                        f"<li>{e.get('degree','') or e} at {e.get('institution','') or ''} ({e.get('year','') or e.get('graduation_year','')})</li>"
                        if isinstance(e, dict) else f"<li>{e}</li>"
                        for e in edu_list
                    )
                    html_body += f"""
                    <hr class="divider">
                    <span class="section-label">Education (unchanged)</span>
                    <ul>{edu_items}</ul>
                    """

                html_body += "</div>"

    html_body += """
        <br>
        <p style="color: #999; font-size: 12px;">
            This is an automated report from the IT Sourcing Agent.<br>
            Tuned resumes are AI-generated to better match the JD — review before sending to clients.
        </p>
    </body>
    </html>
    """

    try:
        ses_client = boto3.client("ses", region_name=region)
        ses_client.send_email(
            Source=sender,
            Destination={"ToAddresses": recipients},
            Message={
                "Subject": {
                    "Data": f"IT Recruitment Report - {total_shortlisted} Shortlisted | {total_tuned} Resumes AI-Tuned",
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
        print(f"[OK] Email sent to {', '.join(recipients)}")
        return {"email_sent": True}

    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_msg = e.response['Error']['Message']
        print(f"WARNING: SES email failed [{error_code}]: {error_msg}")
        print(f"   Sender: {sender}")
        print(f"   Recipients: {recipients}")
        print(f"   Region: {region}")
        return {"email_sent": False}
    except Exception as e:
        print(f"WARNING: Email sending failed: {type(e).__name__}: {e}")
        return {"email_sent": False}
