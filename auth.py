"""Authentication and authorization for HiringFlow AI Simulator.

Two identity sources:
1. Heads sheet → Internal Users (role-based access)
2. Candidates sheet → Candidates (test call only)

Google Sheets is the single source of truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional, Dict, Any

import jwt

BASE_DIR = Path(__file__).resolve().parent
USERS_FILE = BASE_DIR / "data" / "users.json"

JWT_SECRET = os.getenv("JWT_SECRET", secrets.token_urlsafe(48))
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 720  # 30 days


def _create_token(payload: Dict[str, Any]) -> str:
    """Create JWT with given payload."""
    payload["iat"] = int(time.time())
    payload["exp"] = int(time.time()) + (JWT_EXPIRY_HOURS * 3600)
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode and validate JWT, return payload or None."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


# =============================================================================
# Internal User Authentication (Heads Sheet)
# =============================================================================

def create_internal_token(user: Dict[str, Any]) -> str:
    """Create JWT for internal user from Heads sheet."""
    payload = {
        "sub": user['email'],
        "name": user['name'],
        "role": user['role'],
        "user_type": "INTERNAL",
    }
    return _create_token(payload)


def authenticate_internal(email: str, access_code: str) -> Optional[Dict[str, Any]]:
    """Authenticate internal user from Google Sheets Heads tab.

    Returns:
        {"token": "...", "user": {...}} or None
    """
    print(f"[AUTH] authenticate_internal: email={email!r}, access_code={access_code!r}")
    try:
        from sheets import get_sheets_client
        print(f"[AUTH] Importing sheets module...")
        sheets = get_sheets_client()
        print(f"[AUTH] Calling get_heads_user...")
        user = sheets.get_heads_user(email, access_code)
        print(f"[AUTH] get_heads_user returned: {user}")

        if not user:
            print(f"[AUTH] No user found - returning None")
            return None

        print(f"[AUTH] Creating token for user: {user['name']}")
        token = create_internal_token(user)
        print(f"[AUTH] Token created successfully")
        return {
            "token": token,
            "user": {
                "name": user['name'],
                "email": user['email'],
                "queue": user['queue'],
                "role": user['role'],
                "user_type": "INTERNAL",
            }
        }
    except Exception as e:
        print(f"[AUTH] EXCEPTION in authenticate_internal: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return None


# =============================================================================
# Candidate Authentication (Candidates Sheet)
# =============================================================================

def create_candidate_token(candidate: Dict[str, Any]) -> str:
    """Create JWT for candidate from Candidates sheet."""
    payload = {
        "sub": candidate['candidate_id'],
        "name": candidate['candidate_name'],
        "email": candidate['candidate_email'],
        "candidate_id": candidate['candidate_id'],
        "scenario": candidate['scenario'],
        "user_type": "CANDIDATE",
    }
    return _create_token(payload)


def authenticate_candidate(email: str, candidate_id: str) -> Optional[Dict[str, Any]]:
    """Authenticate candidate from Google Sheets Candidates tab.

    Returns:
        {"token": "...", "candidate": {...}, "status": "..."} or None
    """
    print(f"[AUTH] authenticate_candidate: email={email!r}, candidate_id={candidate_id!r}")
    try:
        from sheets import get_sheets_client
        print(f"[AUTH] Importing sheets module...")
        sheets = get_sheets_client()
        print(f"[AUTH] Calling get_candidate...")
        candidate = sheets.get_candidate(email, candidate_id)
        print(f"[AUTH] get_candidate returned: {candidate}")

        if not candidate:
            print(f"[AUTH] No candidate found - returning None")
            return None

        status = candidate.get('test_call_status', 'pending')
        print(f"[AUTH] Candidate status: {status}")

        # If call is already completed or in progress, return status without new token
        if status == 'ended':
            print(f"[AUTH] Call already ended")
            return {
                "status": "ended",
                "message": "You already completed your test call."
            }
        elif status == 'started':
            print(f"[AUTH] Call already in progress")
            return {
                "status": "started",
                "message": "Test call in progress."
            }

        # Create candidate token
        print(f"[AUTH] Creating candidate token...")
        token = create_candidate_token(candidate)
        print(f"[AUTH] Token created successfully")
        return {
            "token": token,
            "candidate": {
                "candidate_id": candidate['candidate_id'],
                "candidate_name": candidate['candidate_name'],
                "scenario": candidate['scenario'],
            },
            "status": "pending"
        }
    except Exception as e:
        print(f"[AUTH] EXCEPTION in authenticate_candidate: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return None


# =============================================================================
# Token Validation
# =============================================================================

def user_from_token(token: str | None) -> Optional[Dict[str, Any]]:
    """Validate JWT and return user payload.

    Returns:
        Decoded JWT payload with user_type, or None if invalid
    """
    if not token:
        return None
    payload = _decode_token(token)
    if not payload:
        return None
    return payload
