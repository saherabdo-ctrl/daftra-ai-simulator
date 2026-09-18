"""Google Drive integration for HiringFlow AI Simulator.

Uploads recordings, transcripts, and evaluation cards to candidate subfolders.
Supports both personal Drive and Shared Drive via SHARED_DRIVE_ID env var.
All uploads are idempotent and retry-capable.
"""

from __future__ import annotations

import os
import logging
import time
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger("drive")

SCOPES = ['https://www.googleapis.com/auth/drive']
FOLDER_NAME = "HiringFlow AI Simulator"

SHARED_DRIVE_ID = os.getenv("SHARED_DRIVE_ID", "").strip()
MAX_UPLOAD_RETRIES = int(os.getenv("DRIVE_UPLOAD_RETRIES", "3"))


def get_drive_client():
    """Get an authorized Google Drive API client."""
    from sheets import load_google_credentials
    creds = load_google_credentials(SCOPES)
    return build("drive", "v3", credentials=creds)


def _find_folder(service, name: str, parent_id: str = None) -> Optional[str]:
    """Find a folder by name under parent. Returns folder ID or None."""
    query = f"mimeType='application/vnd.google-apps.folder' and name='{name}' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"

    kwargs = {
        "fields": "files(id, name)",
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
    }
    if SHARED_DRIVE_ID:
        kwargs["corpora"] = "drive"
        kwargs["driveId"] = SHARED_DRIVE_ID
        if not parent_id:
            query = f"mimeType='application/vnd.google-apps.folder' and name='{name}' and trashed=false"

    results = service.files().list(q=query, **kwargs).execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def _create_folder(service, name: str, parent_id: str = None) -> str:
    """Create a folder and return its ID."""
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]
    elif SHARED_DRIVE_ID:
        metadata["parents"] = [SHARED_DRIVE_ID]

    kwargs = {
        "fields": "id",
        "supportsAllDrives": True,
    }

    folder = service.files().create(body=metadata, **kwargs).execute()
    return folder["id"]


def find_or_create_folder(service, name: str, parent_name: str = None) -> Optional[str]:
    """Find or create a folder hierarchy: parent_name/name. Returns the leaf folder ID."""
    try:
        parent_id = None
        if parent_name:
            parent_id = _find_folder(service, parent_name)
            if not parent_id:
                parent_id = _create_folder(service, parent_name)
                logger.info("Created parent folder: %s (id=%s)", parent_name, parent_id)

        folder_id = _find_folder(service, name, parent_id)
        if not folder_id:
            folder_id = _create_folder(service, name, parent_id)
            logger.info("Created folder: %s (id=%s)", name, folder_id)

        return folder_id
    except Exception as e:
        logger.error("Failed to find/create folder %s: %s", name, e)
        return None


def _file_exists(service, file_name: str, folder_id: str) -> bool:
    """Check if a file with the given name already exists in the folder."""
    try:
        query = f"name='{file_name}' and '{folder_id}' in parents and trashed=false"
        kwargs = {
            "fields": "files(id, name)",
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if SHARED_DRIVE_ID:
            kwargs["corpora"] = "drive"
            kwargs["driveId"] = SHARED_DRIVE_ID

        results = service.files().list(q=query, **kwargs).execute()
        return len(results.get("files", [])) > 0
    except Exception:
        return False


def _upload_with_retry(service, file_path: str, folder_id: str, file_name: str = None, max_retries: int = MAX_UPLOAD_RETRIES) -> Optional[str]:
    """Upload a file with retry logic. Returns the file ID or None."""
    if not file_name:
        file_name = os.path.basename(file_path)

    last_error = None
    for attempt in range(max_retries):
        try:
            if _file_exists(service, file_name, folder_id):
                logger.info("DRIVE UPLOAD SKIP (idempotent): file=%s already exists in folder=%s", file_name, folder_id)
                # Find the existing file ID
                query = f"name='{file_name}' and '{folder_id}' in parents and trashed=false"
                kwargs = {
                    "fields": "files(id)",
                    "supportsAllDrives": True,
                    "includeItemsFromAllDrives": True,
                }
                if SHARED_DRIVE_ID:
                    kwargs["corpora"] = "drive"
                    kwargs["driveId"] = SHARED_DRIVE_ID
                results = service.files().list(q=query, **kwargs).execute()
                existing = results.get("files", [])
                if existing:
                    return existing[0]["id"]

            filename = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)
            metadata = {"name": filename, "parents": [folder_id]}

            ext = os.path.splitext(filename)[1].lower()
            mime_map = {
                ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".json": "application/json",
                ".html": "text/html", ".txt": "text/plain", ".mp4": "video/mp4",
                ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
                ".wav": "audio/wav",
            }
            mime_type = mime_map.get(ext, "application/octet-stream")

            media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)

            kwargs = {
                "fields": "id, name, mimeType, size, webViewLink",
                "supportsAllDrives": True,
            }

            file = service.files().create(body=metadata, media_body=media, **kwargs).execute()
            file_id = file.get("id")
            logger.info("DRIVE UPLOAD SUCCESS: file=%s file_id=%s parent=%s size=%d mime=%s attempt=%d",
                        filename, file_id, folder_id, file_size, mime_type, attempt + 1)
            return file_id
        except Exception as e:
            last_error = e
            logger.warning("DRIVE UPLOAD FAILED (attempt %d/%d): file=%s error=%s",
                           attempt + 1, max_retries, file_path, e)
            if attempt < max_retries - 1:
                time.sleep(3 * (2 ** attempt))

    logger.error("DRIVE UPLOAD FAILED after %d attempts: file=%s error=%s", max_retries, file_path, last_error)
    return None


def upload_file(service, file_path: str, folder_id: str, file_name: str = None, max_retries: int = MAX_UPLOAD_RETRIES) -> Optional[str]:
    """Upload a file to a Drive folder with retry. Returns the file ID."""
    return _upload_with_retry(service, file_path, folder_id, file_name, max_retries)


def upload_audio(service, file_path: str, folder_id: str, max_retries: int = MAX_UPLOAD_RETRIES) -> Optional[str]:
    """Upload audio recording to a Drive folder. Returns the file ID."""
    return _upload_with_retry(service, file_path, folder_id, max_retries=max_retries)


def upload_video(service, file_path: str, folder_id: str, max_retries: int = MAX_UPLOAD_RETRIES) -> Optional[str]:
    """Upload video recording to a Drive folder. Returns the file ID."""
    return _upload_with_retry(service, file_path, folder_id, max_retries=max_retries)


def get_shareable_link(service, candidate_id: str, filename: str, parent_name: str = None) -> Optional[str]:
    """Get a shareable web link for a file in the candidate's Drive folder."""
    try:
        folder_name = candidate_id
        parent_id = None
        if parent_name:
            parent_id = _find_folder(service, parent_name)
        folder_id = _find_folder(service, folder_name, parent_id)

        if not folder_id:
            return None

        query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
        kwargs = {
            "fields": "files(id, webContentLink, webViewLink)",
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if SHARED_DRIVE_ID:
            kwargs["corpora"] = "drive"
            kwargs["driveId"] = SHARED_DRIVE_ID

        results = service.files().list(q=query, **kwargs).execute()
        files = results.get("files", [])

        if files:
            file_id = files[0]["id"]
            perm_kwargs = {"supportsAllDrives": "true"}
            service.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
                **perm_kwargs,
            ).execute()
            file_meta = service.files().get(
                fileId=file_id, fields="webViewLink",
                supportsAllDrives="true"
            ).execute()
            return file_meta.get("webViewLink")

        return None
    except Exception as e:
        logger.error("Failed to get shareable link for %s/%s: %s", candidate_id, filename, e)
        return None


# =============================================================================
# Personal My Drive functions (no Shared Drive corpora/driveId)
# =============================================================================

def find_or_create_folder_personal(service, name: str, parent_id: str) -> Optional[str]:
    """Find or create a folder under a known parent_id on My Drive.

    Unlike find_or_create_folder, this takes a parent folder ID directly
    and does NOT use Shared Drive parameters (corpora, driveId).
    """
    try:
        query = f"mimeType='application/vnd.google-apps.folder' and name='{name}' and '{parent_id}' in parents and trashed=false"
        results = service.files().list(
            q=query,
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = results.get("files", [])
        if files:
            return files[0]["id"]

        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = service.files().create(
            body=metadata,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        folder_id = folder["id"]
        logger.info("Created personal folder: %s (id=%s, parent=%s)", name, folder_id, parent_id)
        return folder_id
    except Exception as e:
        logger.error("Failed to find/create personal folder %s: %s", name, e)
        return None


def upload_file_personal(service, file_path: str, folder_id: str, file_name: str = None) -> Optional[str]:
    """Upload a file to a personal My Drive folder. Returns the file ID or None.

    Does NOT use Shared Drive parameters. Includes idempotent check and retry.
    """
    if not file_name:
        file_name = os.path.basename(file_path)

    for attempt in range(MAX_UPLOAD_RETRIES):
        try:
            # Idempotent: skip if file already exists
            check_query = f"name='{file_name}' and '{folder_id}' in parents and trashed=false"
            check_results = service.files().list(
                q=check_query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            existing = check_results.get("files", [])
            if existing:
                logger.info("PERSONAL UPLOAD SKIP (idempotent): file=%s already exists", file_name)
                return existing[0]["id"]

            filename = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)
            metadata = {"name": filename, "parents": [folder_id]}

            ext = os.path.splitext(filename)[1].lower()
            mime_map = {
                ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".json": "application/json",
                ".html": "text/html", ".txt": "text/plain", ".mp4": "video/mp4",
                ".webm": "video/webm", ".wav": "audio/wav",
            }
            mime_type = mime_map.get(ext, "application/octet-stream")
            media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)

            file = service.files().create(
                body=metadata, media_body=media,
                fields="id, name, mimeType, size, webViewLink",
                supportsAllDrives=True,
            ).execute()
            file_id = file.get("id")
            logger.info("PERSONAL UPLOAD SUCCESS: file=%s file_id=%s parent=%s size=%d attempt=%d",
                        filename, file_id, folder_id, file_size, attempt + 1)
            return file_id
        except Exception as e:
            logger.warning("PERSONAL UPLOAD FAILED (attempt %d/%d): file=%s error=%s",
                           attempt + 1, MAX_UPLOAD_RETRIES, file_path, e)
            if attempt < MAX_UPLOAD_RETRIES - 1:
                time.sleep(3 * (2 ** attempt))

    logger.error("PERSONAL UPLOAD FAILED after %d attempts: file=%s", MAX_UPLOAD_RETRIES, file_path)
    return None


def get_shareable_link_personal(service, folder_id: str, filename: str) -> Optional[str]:
    """Get a shareable web link for a file in a known folder on My Drive.

    Unlike get_shareable_link, this takes a folder ID directly
    and does NOT use Shared Drive parameters.
    """
    try:
        query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
        results = service.files().list(
            q=query,
            fields="files(id, webContentLink, webViewLink)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = results.get("files", [])

        if files:
            file_id = files[0]["id"]
            service.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
                supportsAllDrives=True,
            ).execute()
            file_meta = service.files().get(
                fileId=file_id, fields="webViewLink", supportsAllDrives=True,
            ).execute()
            return file_meta.get("webViewLink")

        return None
    except Exception as e:
        logger.error("Failed to get personal shareable link for %s/%s: %s", folder_id, filename, e)
        return None
