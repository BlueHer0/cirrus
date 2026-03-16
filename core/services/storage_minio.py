"""MinIO S3-compatible storage service for Cirrus.

Handles all object storage operations:
- FIEL files (.cer, .key) — stored under fiel/{rfc}/
- CFDI XMLs — stored under cfdis/{rfc}/{year}/{month}/{tipo}/{uuid}.xml
- Empresa logos — stored under logos/{rfc}/logo.{ext}
"""

import io
import logging
from functools import lru_cache
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from django.conf import settings

logger = logging.getLogger("core.storage")


@lru_cache(maxsize=1)
def _get_client():
    """Create and return a boto3 S3 client configured for MinIO."""
    endpoint = settings.MINIO_ENDPOINT
    # boto3 needs the scheme included
    if not endpoint.startswith("http"):
        endpoint = f"http://{endpoint}"

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.MINIO_ACCESS_KEY,
        aws_secret_access_key=settings.MINIO_SECRET_KEY,
        region_name="us-east-1",  # MinIO default
    )


def _bucket():
    return settings.MINIO_BUCKET


# ── Upload ────────────────────────────────────────────────────────────


def upload_file(local_path: str | Path, minio_key: str, content_type: str = "application/octet-stream") -> str:
    """Upload a local file to MinIO. Returns the key."""
    client = _get_client()
    client.upload_file(
        str(local_path),
        _bucket(),
        minio_key,
        ExtraArgs={"ContentType": content_type},
    )
    logger.info(f"📤 Uploaded: {minio_key}")
    return minio_key


def upload_bytes(data: bytes, minio_key: str, content_type: str = "application/octet-stream") -> str:
    """Upload bytes directly to MinIO. Returns the key."""
    client = _get_client()
    client.put_object(
        Bucket=_bucket(),
        Key=minio_key,
        Body=data,
        ContentType=content_type,
    )
    logger.info(f"📤 Uploaded bytes: {minio_key} ({len(data)} bytes)")
    return minio_key


def upload_fileobj(fileobj, minio_key: str, content_type: str = "application/octet-stream") -> str:
    """Upload a file-like object to MinIO. Returns the key."""
    client = _get_client()
    client.upload_fileobj(
        fileobj,
        _bucket(),
        minio_key,
        ExtraArgs={"ContentType": content_type},
    )
    logger.info(f"📤 Uploaded fileobj: {minio_key}")
    return minio_key


# ── Download ──────────────────────────────────────────────────────────


def download_to_file(minio_key: str, local_path: str | Path) -> Path:
    """Download an object from MinIO to a local file."""
    local_path = Path(local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    client = _get_client()
    client.download_file(_bucket(), minio_key, str(local_path))
    logger.info(f"📥 Downloaded: {minio_key} → {local_path}")
    return local_path


def download_bytes(minio_key: str) -> bytes:
    """Download an object from MinIO as bytes."""
    client = _get_client()
    response = client.get_object(Bucket=_bucket(), Key=minio_key)
    data = response["Body"].read()
    logger.info(f"📥 Downloaded bytes: {minio_key} ({len(data)} bytes)")
    return data


# ── Delete ────────────────────────────────────────────────────────────


def delete_object(minio_key: str) -> bool:
    """Delete an object from MinIO."""
    client = _get_client()
    try:
        client.delete_object(Bucket=_bucket(), Key=minio_key)
        logger.info(f"🗑️ Deleted: {minio_key}")
        return True
    except ClientError as e:
        logger.error(f"Delete failed for {minio_key}: {e}")
        return False


# ── List / Exists ─────────────────────────────────────────────────────


def list_objects(prefix: str, max_keys: int = 1000) -> list[dict]:
    """List objects under a prefix. Returns list of {key, size, last_modified}."""
    client = _get_client()
    response = client.list_objects_v2(Bucket=_bucket(), Prefix=prefix, MaxKeys=max_keys)
    results = []
    for obj in response.get("Contents", []):
        results.append({
            "key": obj["Key"],
            "size": obj["Size"],
            "last_modified": obj["LastModified"].isoformat(),
        })
    return results


def object_exists(minio_key: str) -> bool:
    """Check if an object exists in MinIO."""
    client = _get_client()
    try:
        client.head_object(Bucket=_bucket(), Key=minio_key)
        return True
    except ClientError:
        return False


def get_object_size(minio_key: str) -> int:
    """Get the size of an object in bytes. Returns 0 if not found."""
    client = _get_client()
    try:
        response = client.head_object(Bucket=_bucket(), Key=minio_key)
        return response["ContentLength"]
    except ClientError:
        return 0


# ── FIEL-specific helpers ─────────────────────────────────────────────


def upload_fiel_cer(rfc: str, file_data: bytes) -> str:
    """Upload a FIEL .cer file. Returns the MinIO key."""
    key = f"fiel/{rfc}/{rfc}.cer"
    return upload_bytes(file_data, key, content_type="application/x-x509-ca-cert")


def upload_fiel_key(rfc: str, file_data: bytes) -> str:
    """Upload a FIEL .key file. Returns the MinIO key."""
    key = f"fiel/{rfc}/{rfc}.key"
    return upload_bytes(file_data, key, content_type="application/octet-stream")


def download_fiel_cer(rfc: str, local_path: str | Path) -> Path:
    """Download FIEL .cer to a local path."""
    key = f"fiel/{rfc}/{rfc}.cer"
    return download_to_file(key, local_path)


def download_fiel_key(rfc: str, local_path: str | Path) -> Path:
    """Download FIEL .key to a local path."""
    key = f"fiel/{rfc}/{rfc}.key"
    return download_to_file(key, local_path)


# ── CFDI XML helpers ──────────────────────────────────────────────────


def upload_cfdi_xml(rfc: str, year: int, month: int, tipo: str, uuid: str, xml_data: bytes) -> str:
    """Upload a CFDI XML. Returns the MinIO key.

    Path: cfdis/{rfc}/{year}/{month:02d}/{tipo}/{uuid}.xml
    """
    key = f"cfdis/{rfc}/{year}/{month:02d}/{tipo}/{uuid}.xml"
    return upload_bytes(xml_data, key, content_type="application/xml")


def download_cfdi_xml(minio_key: str) -> bytes:
    """Download a CFDI XML from MinIO as bytes."""
    return download_bytes(minio_key)


# ── Logo helpers ──────────────────────────────────────────────────────


def upload_logo(rfc: str, file_data: bytes, extension: str = "png") -> str:
    """Upload an empresa logo. Returns the MinIO key."""
    key = f"logos/{rfc}/logo.{extension}"
    content_types = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "svg": "image/svg+xml",
        "webp": "image/webp",
    }
    ct = content_types.get(extension.lower(), "image/png")
    return upload_bytes(file_data, key, content_type=ct)


def download_logo(minio_key: str) -> bytes | None:
    """Download a logo from MinIO. Returns None if not found."""
    try:
        return download_bytes(minio_key)
    except ClientError:
        return None
