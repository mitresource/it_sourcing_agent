import os
import json
import boto3
from botocore.exceptions import ClientError


SECRET_NAME = "IT_SOURCING_AGENT"
SECRET_REGION = "ap-south-1"


def load_secrets():
    """
    Fetch OPENAI_API_KEY and MONGO_URI from AWS Secrets Manager
    and inject them into environment variables.

    Falls back to .env values if AWS fetch fails (local dev).
    """
    try:
        session = boto3.session.Session()
        client = session.client(
            service_name="secretsmanager",
            region_name=SECRET_REGION,
        )

        response = client.get_secret_value(SecretId=SECRET_NAME)
        secrets = json.loads(response["SecretString"])

        # Set as env vars so downstream libs (LangChain, Motor) pick them up
        if secrets.get("OPENAI_API_KEY"):
            os.environ["OPENAI_API_KEY"] = secrets["OPENAI_API_KEY"]

        if secrets.get("MONGO_URI"):
            os.environ["MONGO_URI"] = secrets["MONGO_URI"]

        print(f"[OK] Secrets loaded from AWS Secrets Manager ({SECRET_NAME})")

    except ClientError as e:
        print(f"[WARN] AWS Secrets Manager error: {e.response['Error']['Message']}")
        print("   Falling back to .env values")
    except Exception as e:
        print(f"[WARN] Could not load secrets from AWS: {e}")
        print("   Falling back to .env values")
