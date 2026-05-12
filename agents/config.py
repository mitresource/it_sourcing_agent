import os
import json
import boto3
from botocore.exceptions import ClientError


SECRET_NAME = "IT_SOURCING_AGENT"
SECRET_REGION = "ap-south-1"


def load_secrets():
    """
    Fetch all API keys and credentials from AWS Secrets Manager
    and inject them into environment variables.

    Falls back to .env values if AWS fetch fails (local dev).

    Expected keys in the AWS secret:
        OPENAI_API_KEY
        MONGO_URI
        FIRECRAWL_API_KEY
        ANTHROPIC_API_KEY
    """
    try:
        session = boto3.session.Session()
        client = session.client(
            service_name="secretsmanager",
            region_name=SECRET_REGION,
        )

        response = client.get_secret_value(SecretId=SECRET_NAME)
        secrets = json.loads(response["SecretString"])

        # Inject each key into environment if present in the secret
        keys_to_load = [
            "OPENAI_API_KEY",
            "MONGO_URI",
            "FIRECRAWL_API_KEY",
            "CLAUDEAI_API_KEY",
        ]

        loaded = []
        for key in keys_to_load:
            if secrets.get(key):
                os.environ[key] = secrets[key]
                loaded.append(key)

        print(f"[OK] Secrets loaded from AWS Secrets Manager ({SECRET_NAME}): {', '.join(loaded)}")

    except ClientError as e:
        print(f"[WARN] AWS Secrets Manager error: {e.response['Error']['Message']}")
        print("   Falling back to .env values")
    except Exception as e:
        print(f"[WARN] Could not load secrets from AWS: {e}")
        print("   Falling back to .env values")
