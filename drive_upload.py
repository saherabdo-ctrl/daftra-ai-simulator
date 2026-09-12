"""Google Drive integration for HiringFlow AI Simulator.

Uploads recordings, transcripts, and evaluation cards to candidate subfolders.
"""

import os
import logging
from typing import Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger("drive")

SCOPES = ['https://www.googleapis.com/auth/drive']
FOLDER_NAME = "HiringFlow AI Simulator"


def get_drive_client():
    """Get an authorized Google Drive API client."""
    creds_path = os.getenv("GOOGLE_CREDENTIALS", "")
    if not creds_path or not os.path.exists(creds_path):
        raise RuntimeError(f"Google credentials file not found: {creds_path}")

    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def _find_folder(service, name: str, parent_id: str = None) -> Optional[str]:
    """Find a folder by name under parent. Returns folder ID or None."""
    query = f"mimeType='application/vnd.google-apps.folder' and name='{name}' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    else:
        query += " and 'root' in parents"

    results = service.files().list(q=query, fields="files(id, name)").execute()
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

    folder = service.files().create(body=metadata, fields="id").execute()
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


def upload_file(service, file_path: str, folder_id: str) -> Optional[str]:
    """Upload a file to a Drive folder. Returns the file ID."""
    try:
        filename = os.path.basename(file_path)
        metadata = {"name": filename, "parents": [folder_id]}

        # Determine MIME type
        ext = os.path.splitext(filename)[1].lower()
        mime_map = {
            ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg",
            ".json": "application/json",
            ".html": "text/html",
            ".txt": "text/plain",
        }
        mime_type = mime_map.get(ext, "application/octet-stream")

        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        file = service.files().create(body=metadata, media_body=media, fields="id").execute()
        logger.info("Uploaded %s to folder %s (file_id=%s)", filename, folder_id, file["id"])
        return file["id"]
    except Exception as e:
        logger.error("Failed to upload %s: %s", file_path, e)
        return None


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

        # Find the file in the folder
        query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
        results = service.files().list(q=query, fields="files(id, webContentLink, webViewLink)").execute()
        files = results.get("files", [])

        if files:
            file_id = files[0]["id"]
            # Ensure shareable permission
            service.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
            ).execute()
            # Get view link
            file_meta = service.files().get(fileId=file_id, fields="webViewLink").execute()
            return file_meta.get("webViewLink")

        return None
    except Exception as e:
        logger.error("Failed to get shareable link for %s/%s: %s", candidate_id, filename, e)
        return None
