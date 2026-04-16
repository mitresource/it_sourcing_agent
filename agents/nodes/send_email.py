from typing import Dict, Any
from collections import defaultdict
import os
import boto3
from botocore.exceptions import ClientError


async def send_summary_email(state: Dict[str, Any]) -> Dict[str, Any]:
    """Final node: Send shortlisted candidates summary via AWS SES."""

    shortlisted = state.get("shortlisted_candidates", [])
    all_evaluations = state.get("evaluations", [])
    run_id = state.get("run_id", "unknown")
    processed_jobs = state.get("processed_jobs", [])

    sender = os.environ.get("SES_SENDER_EMAIL", "jpagolu@mitresource.com")
    recipients_raw = os.environ.get("SES_RECIPIENT_EMAILS", "akommu@mitresources.com")
    recipients = [e.strip() for e in recipients_raw.split(",") if e.strip()]
    region = os.environ.get("AWS_REGION", "ap-south-1")

    # Group shortlisted candidates by job
    jobs_grouped = defaultdict(list)
    for candidate in shortlisted:
        key = (
            candidate.get("job_id", "unknown"),
            candidate.get("job_title", "Unknown Job"),
            candidate.get("posted_date", ""),
        )
        jobs_grouped[key].append(candidate)

    # Build HTML email body
    total_evaluated = len(all_evaluations)
    total_shortlisted = len(shortlisted)
    total_jobs = len(processed_jobs)

    html_body = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; color: #333; }}
            h2 {{ color: #2c3e50; }}
            h3 {{ color: #34495e; margin-top: 30px; border-bottom: 2px solid #3498db; padding-bottom: 5px; }}
            table {{ border-collapse: collapse; width: 100%; margin-top: 10px; }}
            th {{ background-color: #3498db; color: white; padding: 10px; text-align: left; }}
            td {{ padding: 8px 10px; border-bottom: 1px solid #ddd; }}
            tr:hover {{ background-color: #f5f5f5; }}
            .summary-box {{ background: #eaf2f8; padding: 15px; border-radius: 8px; margin-bottom: 20px; }}
            .score-high {{ color: #27ae60; font-weight: bold; }}
            .score-mid {{ color: #f39c12; font-weight: bold; }}
        </style>
    </head>
    <body>
        <h2>IT Recruitment Agent - Daily Report</h2>
        <div class="summary-box">
            <strong>Run ID:</strong> {run_id}<br>
            <strong>Jobs Processed:</strong> {total_jobs}<br>
            <strong>Total Candidates Evaluated:</strong> {total_evaluated}<br>
            <strong>Total Shortlisted:</strong> {total_shortlisted}
        </div>
    """

    if not shortlisted:
        html_body += "<p>No candidates were shortlisted in this run.</p>"
    else:
        for (job_id, job_title, posted_date), candidates in jobs_grouped.items():
            html_body += f"""
            <h3>{job_title}</h3>
            <p><strong>Job ID:</strong> {job_id} | <strong>Posted:</strong> {posted_date} | <strong>Shortlisted:</strong> {len(candidates)}</p>
            <table>
                <tr>
                    <th>#</th>
                    <th>Candidate Name</th>
                    <th>Email</th>
                    <th>Resume ID</th>
                    <th>Score</th>
                    <th>Reasoning</th>
                </tr>
            """
            sorted_candidates = sorted(candidates, key=lambda x: x.get("match_score", 0), reverse=True)
            for i, c in enumerate(sorted_candidates, 1):
                score = c.get("match_score", 0)
                score_class = "score-high" if score >= 60 else "score-mid"
                html_body += f"""
                <tr>
                    <td>{i}</td>
                    <td>{c.get('candidate_name', 'N/A')}</td>
                    <td>{c.get('candidate_email', 'N/A')}</td>
                    <td>{c.get('resume_id', 'N/A')}</td>
                    <td class="{score_class}">{score}</td>
                    <td>{c.get('reasoning', '')}</td>
                </tr>
                """
            html_body += "</table>"

    html_body += """
        <br>
        <p style="color: #999; font-size: 12px;">This is an automated report from the IT Sourcing Agent.</p>
    </body>
    </html>
    """

    # Send via AWS SES
    try:
        ses_client = boto3.client("ses", region_name=region)

        ses_client.send_email(
            Source=sender,
            Destination={"ToAddresses": recipients},
            Message={
                "Subject": {
                    "Data": f"IT Recruitment Report - {total_shortlisted} Shortlisted across {total_jobs} Jobs",
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
        print(f"⚠️ SES email failed [{error_code}]: {error_msg}")
        print(f"   Sender: {sender}")
        print(f"   Recipients: {recipients}")
        print(f"   Region: {region}")
        return {"email_sent": False}
    except Exception as e:
        print(f"⚠️ Email sending failed: {type(e).__name__}: {e}")
        return {"email_sent": False}
