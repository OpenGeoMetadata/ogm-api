"""S3-compatible storage for immutable generated images (including Cloudflare R2)."""

import base64
import hashlib
import os
from functools import lru_cache
from urllib.parse import quote, urlsplit

import boto3
from botocore.config import Config


def s3_visual_assets_enabled() -> bool:
    return os.getenv("VISUAL_ASSET_DURABLE_STORE", "database").lower() in {"s3", "r2"}


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be configured for visual object storage")
    return value


@lru_cache(maxsize=1)
def _client(process_id: int):
    # Celery forks: create a separate connection pool in each worker process.
    endpoint = _required("VISUAL_ASSET_S3_ENDPOINT")
    if urlsplit(endpoint).scheme != "https":
        raise ValueError("Visual object storage requires an HTTPS endpoint")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=os.getenv("VISUAL_ASSET_S3_REGION", "auto"),
        aws_access_key_id=_required("VISUAL_ASSET_S3_ACCESS_KEY_ID"),
        aws_secret_access_key=_required("VISUAL_ASSET_S3_SECRET_ACCESS_KEY"),
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=20,
            retries={"total_max_attempts": 3, "mode": "standard"},
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def client():
    return _client(os.getpid())


def bucket() -> str:
    return _required("VISUAL_ASSET_S3_BUCKET")


def object_key_for_body(body: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(body).hexdigest()
    prefix = os.getenv("VISUAL_ASSET_S3_PREFIX", "assets").strip("/")
    key = f"{prefix}/{digest}" if prefix else digest
    return key, digest


def upload_visual_asset(body: bytes, content_type: str) -> tuple[str, str]:
    """Publish bytes with a transport checksum; verify metadata before recording a pointer."""
    if not body or not content_type.startswith("image/"):
        raise ValueError("Only nonempty generated image bodies may be uploaded")
    key, digest = object_key_for_body(body)
    s3 = client()
    name = bucket()
    # Repeated uploads have identical bytes and keys, including concurrent renders.
    s3.put_object(
        Bucket=name,
        Key=key,
        Body=body,
        ContentType=content_type,
        ContentMD5=base64.b64encode(hashlib.md5(body).digest()).decode("ascii"),
        CacheControl="public, max-age=31536000, immutable",
        Metadata={"sha256": digest},
    )
    head = s3.head_object(Bucket=name, Key=key)
    if (
        head["ContentLength"] != len(body)
        or head.get("Metadata", {}).get("sha256") != digest
        or head.get("ContentType") != content_type
    ):
        raise RuntimeError("Uploaded visual asset metadata verification failed")
    return key, digest


def read_visual_asset(key: str, digest: str, byte_size: int) -> bytes:
    response = client().get_object(Bucket=bucket(), Key=key)
    with response["Body"] as stream:
        # Read one extra byte to detect unexpected data without unbounded allocation.
        body = stream.read(byte_size + 1)
    if len(body) != byte_size or hashlib.sha256(body).hexdigest() != digest:
        raise RuntimeError("Visual asset checksum verification failed")
    return body


def public_visual_asset_url(key: str) -> str | None:
    base = os.getenv("VISUAL_ASSET_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base:
        return None
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("VISUAL_ASSET_PUBLIC_BASE_URL must be an HTTPS base URL")
    return f"{base}/{quote(key, safe='/')}"
