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

JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 720  # 30 days

# =============================================================================
# Roles & Permissions
# =============================================================================
# من هنا لغاية آخر القسم ده: الصلاحيات الفعلية بقت مخزّنة لكل مستخدم على حدة
# (عمود Permissions في شيت Heads)، مش مشتقة من الـ Role. الـ Role بقى مجرد
# مسمى وظيفي/تنظيمي (زي "EG SDR") مالوش أي تأثير على الصلاحيات — الاتنين
# منفصلين تمامًا زي ما اتفقنا.

# المسميات الوظيفية المتاحة (للعرض في الفورم بس — مالهاش علاقة بالصلاحيات)
USER_ROLES: list[str] = [
    "Quality User",
    "EG Sales",
    "EG Sales Leader",
    "EG Sales Manager",
    "Global Sales",
    "Global Sales Leader",
    "Sales Manager",
    "SDR Manager",
    "SDR",
    "KSA Sales Manager",
    "KSA Sales",
    "KSA SDR",
]

# كل الصلاحيات المتاحة في النظام — checkbox لكل واحدة في فورم المستخدم
PERMISSION_KEYS: list[str] = [
    "manage_users",
    "manage_classifications",
    "manage_ai_clients",
    "delete_calls_results",
    "view_results",
    "view_recordings",
    "generate_test_links",
    "make_calls",
]


def empty_permissions() -> Dict[str, bool]:
    return {key: False for key in PERMISSION_KEYS}


def full_permissions() -> Dict[str, bool]:
    return {key: True for key in PERMISSION_KEYS}


def normalize_permissions(data: Dict[str, Any] | None) -> Dict[str, bool]:
    """يحوّل أي dict جزئي لصلاحيات كاملة (كل مفتاح مش موجود = False)."""
    data = data or {}
    return {key: bool(data.get(key, False)) for key in PERMISSION_KEYS}


if not JWT_SECRET:
    JWT_SECRET = secrets.token_urlsafe(48)
    print("[AUTH] WARNING: JWT_SECRET not set in .env — generated random secret. "
          "Tokens will be invalidated on restart. Set JWT_SECRET in .env for persistence.")


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
    """Create JWT for internal user from Heads sheet.

    user لازم يكون فيه permissions (dict) وall_classifications (bool) و
    classification_ids (list[str]) — دول مخزّنين لكل مستخدم على حدة في الشيت،
    مش مشتقين من role."""
    payload = {
        "sub": user['email'],
        "name": user['name'],
        "role": user.get('role', ''),
        "user_type": "INTERNAL",
        "permissions": normalize_permissions(user.get('permissions')),
        "all_classifications": bool(user.get('all_classifications', False)),
        "classification_ids": user.get('classification_ids') or [],
    }
    return _create_token(payload)


def authenticate_internal(email: str, access_code: str) -> Optional[Dict[str, Any]]:
    """Authenticate internal user from Google Sheets Heads tab.

    Returns:
        {"token": "...", "user": {...}} or None
    """
    print(f"[AUTH] authenticate_internal: email={email!r}, access_code={access_code!r}")

    override_email = os.getenv("ADMIN_OVERRIDE_EMAIL", "")
    override_code = os.getenv("ADMIN_OVERRIDE_CODE", "")
    if override_email and override_code and email == override_email and access_code == override_code:
        print(f"[AUTH] Matched ADMIN_OVERRIDE_EMAIL — bypassing Heads sheet lookup")
        # حساب الطوارئ (bootstrap) — صلاحيات كاملة دايمًا بغض النظر عن الشيت،
        # عشان تفضل قادر تدخل حتى لو حصل خطأ في صف المستخدم بتاعك في الشيت.
        user = {
            "name": "Admin",
            "email": override_email,
            "queue": "",
            "role": "ADMIN",
            "permissions": full_permissions(),
            "all_classifications": True,
            "classification_ids": [],
        }
        token = create_internal_token(user)
        return {
            "token": token,
            "user": {
                "name": user["name"],
                "email": user["email"],
                "queue": user["queue"],
                "role": user["role"],
                "user_type": "INTERNAL",
                "permissions": user["permissions"],
                "all_classifications": user["all_classifications"],
                "classification_ids": user["classification_ids"],
            }
        }

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
                "permissions": normalize_permissions(user.get('permissions')),
                "all_classifications": bool(user.get('all_classifications', False)),
                "classification_ids": user.get('classification_ids') or [],
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
# Unified Login (Heads sheet first, then Candidates sheet)
# =============================================================================

def authenticate_user(email: str, code: str) -> Optional[Dict[str, Any]]:
    """Single login entry point used by the merged login page.

    Tries the Heads sheet (internal staff) first — if the email+code match an
    active Heads row, the user logs in with their Role/permissions exactly as
    authenticate_internal() already does. Only if no Heads match is found do we
    fall back to the Candidates sheet. This order means a Heads row always wins
    if the same email happens to exist in both sheets.

    Returns whatever the matching branch returns (internal: {"token","user"};
    candidate: {"token","candidate","status"} or a no-token {"status","message"}
    for an already-ended/in-progress call), or None if neither sheet matches.
    """
    internal = authenticate_internal(email, code)
    if internal:
        return internal
    return authenticate_candidate(email, code)


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
