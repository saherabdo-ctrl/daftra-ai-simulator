"""HiringFlow integration module.

Handles sending test call results (including files) to HiringFlow webhook.
HiringFlow handles Google Drive upload — no service account needed.
"""

import asyncio
import base64
import json
import logging
import os
import re
from pathlib import Path

import httpx

logger = logging.getLogger("hiringflow-integration")

HIRINGFLOW_WEBHOOK_URL = os.getenv("HIRINGFLOW_WEBHOOK_URL", "").strip()
HIRINGFLOW_WEBHOOK_SECRET = os.getenv("HIRINGFLOW_WEBHOOK_SECRET", "").strip()

# Cache for dynamically fetched webhook URL
_fetched_webhook_url: str | None = None


async def fetch_webhook_url_from_script(apps_script_url: str) -> str | None:
    """Fetch the HiringFlow webhook URL from Apps Script properties.

    The Apps Script must have a doGet() function that returns
    PropertiesService.getScriptProperties().getProperty('HIRINGFLOW_WEBHOOK_URL')

    Args:
        apps_script_url: The deployed Apps Script web app URL

    Returns:
        The webhook URL from script properties, or None on failure
    """
    global _fetched_webhook_url

    if not apps_script_url:
        return None

    try:
        # Call the Apps Script with action=getWebhookUrl
        params = {"action": "getWebhookUrl"}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(apps_script_url, params=params)
            if response.status_code == 200:
                data = response.json()
                url = data.get("webhook_url", "").strip()
                if url:
                    _fetched_webhook_url = url
                    logger.info("Fetched webhook URL from Apps Script: %s", url[:60] + "...")
                    return url
                else:
                    logger.warning("Apps Script returned empty webhook_url")
            else:
                logger.warning("Apps Script returned HTTP %s", response.status_code)
    except Exception as e:
        logger.warning("Failed to fetch webhook URL from Apps Script: %s", e)

    return None


def get_webhook_url() -> str:
    """Get the active webhook URL.

    Priority: dynamically fetched > env var HIRINGFLOW_WEBHOOK_URL
    """
    if _fetched_webhook_url:
        return _fetched_webhook_url
    return HIRINGFLOW_WEBHOOK_URL


def _read_file_base64(path: str) -> str | None:
    """Read a file and return base64-encoded content."""
    if not path or not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


async def send_result_to_hiringflow(
    test_session_id: str,
    score: int,
    evaluation: dict,
    transcript: list,
    recording_path: str | None = None,
    max_retries: int = 3,
) -> bool:
    """Send test call results to HiringFlow webhook.

    Files are base64-encoded and sent in the webhook payload.
    HiringFlow handles Google Drive upload.

    Args:
        test_session_id: e.g. "TEST-A1B2C3D4"
        score: overall_score (0-100)
        evaluation: Full evaluation dict
        transcript: List of {role, text} dicts
        recording_path: Path to MP3 file (or None)
        max_retries: Number of retry attempts

    Returns:
        True if successful, False otherwise
    """
    webhook_url = get_webhook_url()
    if not webhook_url:
        logger.error("HIRINGFLOW_WEBHOOK_URL not configured — cannot send results")
        return False

    payload = {
        "secret": HIRINGFLOW_WEBHOOK_SECRET,
        "test_session_id": test_session_id,
        "score": score,
        "evaluation": evaluation,
        "transcript": transcript,
    }

    # Encode recording as base64
    if recording_path:
        recording_b64 = _read_file_base64(recording_path)
        if recording_b64:
            payload["recording_base64"] = recording_b64
            payload["recording_filename"] = os.path.basename(recording_path)
            logger.info("Included recording in payload (%d bytes base64)", len(recording_b64))

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                if response.status_code == 200:
                    data = response.json()
                    logger.info("Successfully sent results to HiringFlow for session %s", test_session_id)
                    return True
                elif response.status_code in (400, 401, 404, 409):
                    logger.error("Permanent error from HiringFlow (HTTP %s): %s", response.status_code, response.text)
                    return False
                else:
                    logger.warning("HiringFlow returned HTTP %s (attempt %d/%d)", response.status_code, attempt + 1, max_retries)
        except Exception as e:
            logger.warning("Failed to send to HiringFlow (attempt %d/%d): %s", attempt + 1, max_retries, e)

        if attempt < max_retries - 1:
            await asyncio.sleep(5 * (2 ** attempt))

    logger.error("Failed to send results to HiringFlow after %d attempts", max_retries)
    return False
