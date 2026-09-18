"""HiringFlow integration module.

Sends test call results (including recording URLs) to HiringFlow webhook.
AI Simulator owns all recordings and uploads them to Google Drive.
Webhook payload contains audio/video recording metadata and URLs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from typing import Optional

import httpx

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("hiringflow-integration")

HIRINGFLOW_WEBHOOK_URL = os.getenv("HIRINGFLOW_WEBHOOK_URL", "").strip()
HIRINGFLOW_WEBHOOK_SECRET = os.getenv("HIRINGFLOW_WEBHOOK_SECRET", "").strip()

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
        params = {"action": "getWebhookUrl"}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(apps_script_url, params=params)
            content_type = response.headers.get("content-type", "")
            status = response.status_code
            body_preview = response.text[:200] if response.text else "(empty)"

            if status != 200:
                logger.warning("Apps Script returned HTTP %s (Content-Type: %s)", status, content_type)
                return None

            # Apps Script returns HTML execution page for GET requests
            # Only parse as JSON if Content-Type indicates JSON
            if "application/json" not in content_type:
                logger.info("Apps Script returned non-JSON response (HTTP %s, Content-Type: %s). "
                            "Using env HIRINGFLOW_WEBHOOK_URL.", status, content_type)
                return None

            data = response.json()
            url = data.get("webhook_url", "").strip()
            if url:
                _fetched_webhook_url = url
                logger.info("Fetched webhook URL from Apps Script: %s", url[:60] + "...")
                return url
            else:
                logger.warning("Apps Script returned empty webhook_url")
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


def _verify_webhook_secret(payload: dict) -> bool:
    """Verify the webhook secret in the payload."""
    secret = payload.get("secret", "")
    if not secret or not HIRINGFLOW_WEBHOOK_SECRET:
        return False
    return secrets.compare_digest(secret, HIRINGFLOW_WEBHOOK_SECRET)


async def send_result_to_hiringflow(
    test_session_id: str,
    candidate_id: str,
    score: int,
    evaluation: dict,
    transcript: list,
    drive_folder_id: Optional[str] = None,
    audio_recording: Optional[dict] = None,
    video_recording: Optional[dict] = None,
    max_retries: int = 3,
) -> dict:
    """Send test call results to HiringFlow webhook with recording URLs.

    Args:
        test_session_id: e.g. "TEST-A1B2C3D4"
        candidate_id: e.g. "CAND-A1B2C3D4"
        score: overall_score (0-100)
        evaluation: Full evaluation dict
        transcript: List of {role, text} dicts
        drive_folder_id: Google Drive folder ID for this candidate
        audio_recording: {"status": "uploaded", "file_id": "...", "url": "..."} or None
        video_recording: {"status": "uploaded", "file_id": "...", "url": "..."} or None
        max_retries: Number of retry attempts

    Returns:
        Dict with "success" (bool), "attempts" (int), and "error" (str or None)
    """
    webhook_url = get_webhook_url()
    if not webhook_url:
        logger.error("HIRINGFLOW_WEBHOOK_URL not configured — cannot send results")
        return {"success": False, "attempts": 0, "error": "HIRINGFLOW_WEBHOOK_URL not configured"}

    payload = {
        "secret": HIRINGFLOW_WEBHOOK_SECRET,
        "test_session_id": test_session_id,
        "candidate_id": candidate_id,
        "event": "ended",
        "score": score,
        "evaluation": evaluation,
        "transcript": transcript,
    }

    # Add Drive folder ID and recording metadata
    if drive_folder_id:
        payload["drive_folder_id"] = drive_folder_id
    if audio_recording:
        payload["audio_recording"] = audio_recording
    if video_recording:
        payload["video_recording"] = video_recording

    last_error = None
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
                    return {"success": True, "attempts": attempt + 1, "error": None}
                elif response.status_code in (400, 401, 404, 409):
                    logger.error("Permanent error from HiringFlow (HTTP %s): %s", response.status_code, response.text)
                    return {"success": False, "attempts": attempt + 1, "error": f"HTTP {response.status_code}: {response.text}"}
                else:
                    logger.warning("HiringFlow returned HTTP %s (attempt %d/%d)", response.status_code, attempt + 1, max_retries)
        except Exception as e:
            last_error = str(e)
            logger.warning("Failed to send to HiringFlow (attempt %d/%d): %s", attempt + 1, max_retries, e)

        if attempt < max_retries - 1:
            await asyncio.sleep(5 * (2 ** attempt))

    logger.error("Failed to send results to HiringFlow after %d attempts", max_retries)
    return {"success": False, "attempts": max_retries, "error": last_error}
