import sys
import os
import json
import re

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(override=True)

from agents.config import load_secrets
load_secrets()

import boto3
from botocore.exceptions import ClientError
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("resend_email")

CACHE_FILE = os.path.join(os.path.dirname(__file__), "last_run_email_cache.json")

# Default recipients for resend — override via CLI args
DEFAULT_RECIPIENTS = [
    "mperungulam@mitresource.com",
    "slabhishetty@mitresource.com",
    "yaseen@mitresource.com",
    "rajkumar@mitresource.com",
    "raju.kaguturi@mitresource.com",
    "mani@mitresource.com",
    "jagadiahjagu@gmail.com"
]


def clean_html(html: str) -> str:
    # Remove "Total Shortlisted" and "Resumes AI-Tuned" lines from summary box
    html = re.sub(r'<strong>Total Shortlisted:</strong>[^<]*<br>\s*', '', html)
    html = re.sub(r'<strong>Resumes AI-Tuned:</strong>[^<]*', '', html)
    # Remove Experience section from each tuned-box (label + ul)
    html = re.sub(
        r'<span class="section-label">Experience</span>\s*<ul[^>]*>.*?</ul>',
        '',
        html,
        flags=re.DOTALL,
    )
    return html


def resend():
    recipients = [r for r in (sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_RECIPIENTS) if r]

    if not os.path.exists(CACHE_FILE):
        logger.error(f"No cache file found at {CACHE_FILE}. Run runner.py first.")
        sys.exit(1)

    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        cache = json.load(f)

    subject = cache["subject"]
    html_body = clean_html(cache["html_body"])
    run_id = cache.get("run_id", "unknown")

    sender = os.environ.get("SES_SENDER_EMAIL", "")
    region = os.environ.get("AWS_SES_REGION", os.environ.get("AWS_REGION", "us-west-2"))

    logger.info(f"Resending run {run_id} to: {', '.join(recipients)}")

    try:
        ses_client = boto3.client("ses", region_name=region)
        ses_client.send_email(
            Source=sender,
            Destination={"ToAddresses": recipients},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Html": {"Data": html_body, "Charset": "UTF-8"}},
            },
        )
        logger.info(f"Email resent successfully to {', '.join(recipients)}")

    except ClientError as e:
        logger.error(f"SES error: {e.response['Error']['Code']} — {e.response['Error']['Message']}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    resend()
