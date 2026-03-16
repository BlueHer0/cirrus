"""FIEL credential encryption and management.

Uses Fernet symmetric encryption to protect FIEL passwords.
The encryption key comes from settings.FIEL_ENCRYPTION_KEY.

SECURITY RULES:
- Password is NEVER stored in plain text in the database
- .cer/.key files are stored ONLY in MinIO, never on local disk permanently
- During scraping, files are downloaded to /tmp and deleted after use
- FIEL credentials are NEVER exposed via API to external apps
- Logs NEVER contain the password
"""

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

logger = logging.getLogger("core.fiel")


def _get_fernet() -> Fernet:
    """Get a Fernet instance using the configured key."""
    key = settings.FIEL_ENCRYPTION_KEY
    if not key:
        raise ValueError("FIEL_ENCRYPTION_KEY is not configured in settings")
    if isinstance(key, str):
        key = key.encode("utf-8")
    return Fernet(key)


def encrypt_password(password: str) -> bytes:
    """Encrypt a FIEL password. Returns encrypted bytes for storage in BinaryField."""
    f = _get_fernet()
    return f.encrypt(password.encode("utf-8"))


def decrypt_password(encrypted: bytes) -> str:
    """Decrypt a FIEL password from stored bytes."""
    f = _get_fernet()
    try:
        # Handle memoryview from Django BinaryField
        if isinstance(encrypted, memoryview):
            encrypted = bytes(encrypted)
        return f.decrypt(encrypted).decode("utf-8")
    except InvalidToken:
        raise ValueError("Failed to decrypt FIEL password — invalid key or corrupted data")


def upload_fiel(empresa, cer_data: bytes, key_data: bytes, password: str) -> dict:
    """Upload FIEL credentials for an empresa.

    Args:
        empresa: Empresa model instance
        cer_data: Raw bytes of the .cer file
        key_data: Raw bytes of the .key file
        password: Plain text password

    Returns:
        dict with cer_key, key_key, and fiel metadata
    """
    from .storage_minio import upload_fiel_cer, upload_fiel_key

    # 1. Upload files to MinIO
    cer_minio_key = upload_fiel_cer(empresa.rfc, cer_data)
    key_minio_key = upload_fiel_key(empresa.rfc, key_data)

    # 2. Encrypt password
    encrypted_password = encrypt_password(password)

    # 3. Validate FIEL locally (extract RFC and expiration)
    fiel_info = validate_fiel_local(cer_data, key_data, password)

    # 4. Update empresa model
    empresa.fiel_cer_key = cer_minio_key
    empresa.fiel_key_key = key_minio_key
    empresa.fiel_password_encrypted = encrypted_password
    empresa.fiel_expira = fiel_info.get("valid_to")
    empresa.save(update_fields=[
        "fiel_cer_key", "fiel_key_key", "fiel_password_encrypted", "fiel_expira",
    ])

    logger.info("🔐 FIEL uploaded for %s (expires: %s)", empresa.rfc, fiel_info.get("valid_to"))

    return {
        "cer_key": cer_minio_key,
        "key_key": key_minio_key,
        "rfc_from_cert": fiel_info.get("rfc"),
        "valid_to": fiel_info.get("valid_to"),
        "is_valid": fiel_info.get("is_valid"),
    }


def validate_fiel_local(cer_data: bytes, key_data: bytes, password: str) -> dict:
    """Validate FIEL credentials locally (without logging into SAT).

    Writes files to temp, loads with FIELLoader, extracts RFC and expiry.
    """
    from sat_scrapper_core import FIELLoader

    with tempfile.TemporaryDirectory(prefix="cirrus_fiel_") as tmpdir:
        cer_path = Path(tmpdir) / "cert.cer"
        key_path = Path(tmpdir) / "cert.key"
        cer_path.write_bytes(cer_data)
        key_path.write_bytes(key_data)

        try:
            fiel = FIELLoader(str(cer_path), str(key_path), password)
            return {
                "rfc": fiel.rfc,
                "valid_to": fiel.valid_to,
                "is_valid": fiel.is_valid,
            }
        except Exception as e:
            logger.error("FIEL validation failed: %s", e)
            raise ValueError(f"FIEL validation failed: {e}")
        # tmpdir is auto-deleted here — files never persist on disk


def get_fiel_for_scraping(empresa) -> dict:
    """Download FIEL files from MinIO to a temporary directory for scraping.

    Returns dict with temp_dir (TemporaryDirectory), cer_path, key_path, password.
    The caller MUST clean up temp_dir when done.

    Usage:
        fiel_ctx = get_fiel_for_scraping(empresa)
        try:
            # use fiel_ctx["cer_path"], fiel_ctx["key_path"], fiel_ctx["password"]
        finally:
            fiel_ctx["temp_dir"].cleanup()
    """
    from .storage_minio import download_to_file

    if not empresa.fiel_cer_key or not empresa.fiel_key_key:
        raise ValueError(f"FIEL not configured for {empresa.rfc}")
    if not empresa.fiel_password_encrypted:
        raise ValueError(f"FIEL password not set for {empresa.rfc}")

    temp_dir = tempfile.TemporaryDirectory(prefix="cirrus_scrape_")
    tmppath = Path(temp_dir.name)

    cer_path = download_to_file(empresa.fiel_cer_key, tmppath / "cert.cer")
    key_path = download_to_file(empresa.fiel_key_key, tmppath / "cert.key")
    password = decrypt_password(empresa.fiel_password_encrypted)

    return {
        "temp_dir": temp_dir,
        "cer_path": str(cer_path),
        "key_path": str(key_path),
        "password": password,
    }


async def verify_fiel_sat(empresa) -> dict:
    """Test FIEL login against the SAT portal.

    Downloads FIEL to temp, uses SATEngine.verify_login(), returns result.
    Updates empresa.fiel_verificada and fiel_verificada_at.
    """
    from asgiref.sync import sync_to_async
    from sat_scrapper_core import SATEngine, ScrapeConfig

    fiel_ctx = get_fiel_for_scraping(empresa)
    try:
        config = ScrapeConfig(
            cer_path=fiel_ctx["cer_path"],
            key_path=fiel_ctx["key_path"],
            password=fiel_ctx["password"],
            year=2025,
            month_start=1,
            month_end=1,
            tipos=["recibidos"],
            headless=True,
        )

        async with SATEngine(config) as engine:
            ok = await engine.verify_login()

        if ok:
            empresa.fiel_verificada = True
            empresa.fiel_verificada_at = datetime.now(timezone.utc)
            await sync_to_async(empresa.save)(update_fields=["fiel_verificada", "fiel_verificada_at"])
            logger.info("✅ FIEL verified for %s", empresa.rfc)
            return {"verified": True, "rfc": empresa.rfc}
        else:
            empresa.fiel_verificada = False
            await sync_to_async(empresa.save)(update_fields=["fiel_verificada"])
            logger.warning("❌ FIEL verification failed for %s", empresa.rfc)
            return {"verified": False, "error": "SAT login failed"}

    except Exception as e:
        empresa.fiel_verificada = False
        await sync_to_async(empresa.save)(update_fields=["fiel_verificada"])
        logger.warning("❌ FIEL verification failed for %s: %s", empresa.rfc, e)
        return {"verified": False, "error": str(e)}
    finally:
        fiel_ctx["temp_dir"].cleanup()
