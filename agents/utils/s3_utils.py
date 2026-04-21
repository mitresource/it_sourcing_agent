import json
import os
import boto3
from botocore.exceptions import ClientError

S3_BUCKET = os.environ.get("S3_BUCKET_NAME", "it-sourcing-agent-resumes")
S3_REGION = os.environ.get("AWS_REGION", "ap-south-1")


def _s3():
    return boto3.client("s3", region_name=S3_REGION)


def upload_resume_json(resume_dict: dict, s3_key: str) -> str | None:
    """Upload a resume dict as JSON to S3. Returns the S3 URI or None on failure."""
    try:
        body = json.dumps(resume_dict, indent=2, ensure_ascii=False).encode("utf-8")
        _s3().put_object(
            Bucket=S3_BUCKET,
            Key=s3_key,
            Body=body,
            ContentType="application/json",
        )
        return f"s3://{S3_BUCKET}/{s3_key}"
    except ClientError as e:
        print(f"WARNING: S3 upload failed [{s3_key}]: {e}")
        return None


def generate_presigned_url(s3_key: str, expiry_seconds: int = 604800) -> str | None:
    """Return a pre-signed HTTPS URL valid for 7 days (default)."""
    try:
        return _s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": s3_key},
            ExpiresIn=expiry_seconds,
        )
    except ClientError as e:
        print(f"WARNING: Pre-signed URL failed [{s3_key}]: {e}")
        return None


def formatted_s3_key(job_id: str, candidate_name: str) -> str:
    safe = candidate_name.replace(" ", "_")
    return f"resumes/formatted/{job_id}/{safe}_formatted.json"


def tuned_s3_key(job_id: str, candidate_name: str) -> str:
    safe = candidate_name.replace(" ", "_")
    return f"resumes/tuned/{job_id}/{safe}_tuned.json"
