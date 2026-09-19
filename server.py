"""الخادم الخلفي لـ Daftra AI-Simulator.

يقدّم لوحة التحكم، ويصدر رموز LiveKit (مع إرسال العميل الآلي تلقائيًا)،
ويخزّن/يعيد نتائج التقييم المرسلة من العميل الآلي.

التشغيل:  python server.py
"""

import json
import logging
import os
import random
import re
import secrets
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("server")

# Ensure UTF-8 output on all platforms (Windows cp1252 crashes on Arabic)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import auth
import call_learning
import personas
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from livekit.api import AccessToken, RoomAgentDispatch, RoomConfiguration, VideoGrants
from pydantic import BaseModel

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data" / "results"
SCENARIOS_DIR = BASE_DIR / "data" / "scenarios"
CALLS_DIR = BASE_DIR / "data" / "calls"
DAFTRA_KNOWLEDGE_PATH = BASE_DIR / "data" / "daftra_knowledge.md"

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "").strip()
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "").strip()
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "").strip()
AGENT_NAME = os.getenv("AGENT_NAME", "daftra-ai-simulator")
WEBHOOK_SECRET = os.getenv("AGENT_WEBHOOK_SECRET", "")
HIRINGFLOW_API_KEY = os.getenv("HIRINGFLOW_API_KEY", "").strip()
PORT = int(os.getenv("PORT", "8000"))

# Validate LiveKit configuration at startup
_livekit_errors = []
if not LIVEKIT_URL:
    _livekit_errors.append("LIVEKIT_URL is not set")
elif "your-project" in LIVEKIT_URL or "your_" in LIVEKIT_URL:
    _livekit_errors.append("LIVEKIT_URL contains placeholder — set real URL from LiveKit Cloud dashboard")
if not LIVEKIT_API_KEY:
    _livekit_errors.append("LIVEKIT_API_KEY is not set")
elif LIVEKIT_API_KEY.startswith("your_") or len(LIVEKIT_API_KEY) < 10:
    _livekit_errors.append("LIVEKIT_API_KEY appears to be a placeholder")
if not LIVEKIT_API_SECRET:
    _livekit_errors.append("LIVEKIT_API_SECRET is not set")
elif LIVEKIT_API_SECRET.startswith("your_") or len(LIVEKIT_API_SECRET) < 10:
    _livekit_errors.append("LIVEKIT_API_SECRET appears to be a placeholder")

if _livekit_errors:
    print("=" * 60)
    print("WARNING: LiveKit configuration issues — token generation will fail")
    for e in _livekit_errors:
        print(f"  ✗ {e}")
    print("  Set these in your deployment dashboard (NOT in source code)")
    print("=" * 60)
else:
    print("[server] LiveKit configuration validated ✓")

app = FastAPI(title="Daftra AI-Simulator")

_results: dict[str, dict] = {}
_custom_briefs: dict[str, dict] = {}
_trial_rooms: set[str] = set()  # Rooms created via trial/attempt links (evaluation visible)

# تحميل السيناريوهات المخصصة المحفوظة عند إعادة التشغيل.
SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
for f in SCENARIOS_DIR.glob("*.json"):
    try:
        brief = json.loads(f.read_text(encoding="utf-8"))
        _custom_briefs[brief["id"]] = brief
    except Exception:
        pass


class TokenRequest(BaseModel):
    scenario: str = "new-lead-discovery-call"


class StartSessionRequest(BaseModel):
    test_session_id: str
    candidate_name: str
    queue: str = "SDR"
    scenario_id: str = ""


class CustomScenarioRequest(BaseModel):
    name: str = ""
    customer_name: str = ""
    customer_role: str = ""
    company_name: str = ""
    business_field: str = ""
    company_size: str = ""
    pain_point: str = ""
    decision_maker: bool = True
    budget_sensitivity: str = "متوسطة"
    buying_intent: str = "متوسطة"
    difficulty: str = "medium"
    temperature: str = "warm"
    product_brief: str = ""
    persona: str = ""
    approximate_age: str = ""
    dialect: str = "saudi"
    knowledgeable: bool = False


class LoginRequest(BaseModel):
    username: str
    password: str


class CandidateLoginRequest(BaseModel):
    email: str
    candidate_id: str


class CandidateStartCallRequest(BaseModel):
    attempt_id: str = ""  # Optional — only set for trial/attempt flow


def _scenario_exists(scenario_id: str) -> bool:
    if scenario_id.startswith("custom_"):
        return scenario_id in _custom_briefs
    return scenario_id in personas.SCENARIOS


def require_user(authorization: str = Header(None)) -> dict:
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    user = auth.user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="يجب تسجيل الدخول")
    return user


def require_permission(permission_key: str):
    """يبني Dependency بيتأكد إن المستخدم عنده صلاحية معينة (مخزّنة في ملفه الشخصي،
    مش مشتقة من role). كل endpoint بيحدد الصلاحية المناسبة له صراحة."""
    def _dependency(user: dict = Depends(require_user)) -> dict:
        if not user.get("permissions", {}).get(permission_key):
            raise HTTPException(status_code=403, detail=f"هذه العملية تتطلب صلاحية: {permission_key}")
        return user
    return _dependency


def scoped_classification_ids(user: dict) -> list[str] | None:
    """يرجّع قايمة التصنيفات المسموح للمستخدم يشوفها، أو None لو مسموحله بكل حاجة."""
    if user.get("all_classifications"):
        return None
    return user.get("classification_ids") or []


def in_classification_scope(classification_id: str, scoped_ids: list[str] | None) -> bool:
    """scoped_ids=None يعني مفيش تقييد. classification_id فاضي (بيانات قديمة قبل
    الميزة دي) بيفضل ظاهر للجميع بدل ما يختفي بغتة."""
    if scoped_ids is None or not classification_id:
        return True
    return classification_id in scoped_ids


def require_user_or_service(authorization: str = Header(None)) -> dict:
    """يقبل مستخدمًا مسجّلًا، أو العميل الآلي عبر AGENT_WEBHOOK_SECRET (كخدمة داخلية)."""
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if token == WEBHOOK_SECRET:
        return {"username": "__agent__", "name": "العميل الآلي", "role": "service"}
    user = auth.user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="يجب تسجيل الدخول")
    return user


def require_hiringflow_api_key(authorization: str = Header(None)) -> bool:
    """Validate HiringFlow API key for /api/start-session endpoint."""
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if not HIRINGFLOW_API_KEY:
        raise HTTPException(status_code=500, detail="HIRINGFLOW_API_KEY not configured")
    if token != HIRINGFLOW_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True


def _all_scenario_summaries() -> list[dict]:
    scenarios = personas.scenarios_summary()
    for brief in _custom_briefs.values():
        scenarios.append(personas.custom_scenario_summary(personas.build_custom_scenario(brief)))
    return scenarios


@app.post("/api/login")
async def login(req: LoginRequest) -> dict:
    """Unified login: checks the Heads sheet (internal staff) first, then the
    Candidates sheet, so one login page serves both internal users and candidates."""
    print(f"[LOGIN] Attempt: username={req.username!r}")
    try:
        result = auth.authenticate_user(req.username.strip(), req.password)
        if result:
            if "user" in result:
                print(f"[LOGIN] Success (internal): user={result['user']['name']}, role={result['user']['role']}")
            else:
                print(f"[LOGIN] Success/status (candidate): status={result.get('status', 'pending')}")
            return result
        print(f"[LOGIN] FAILED: no match in Heads or Candidates")
        raise HTTPException(status_code=401, detail="Invalid email or access code")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[LOGIN] EXCEPTION: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Login error: {type(e).__name__}: {str(e)[:200]}")


# =============================================================================
# Candidate Endpoints (Test Call Only)
# =============================================================================

@app.post("/api/candidate/login")
async def candidate_login(req: CandidateLoginRequest) -> dict:
    """Candidate login using Google Sheets Candidates tab.

    CandidateID IS the access code.
    """
    print(f"[CANDIDATE LOGIN] Attempt: email={req.email!r}, candidate_id={req.candidate_id!r}")
    try:
        result = auth.authenticate_candidate(req.email.strip(), req.candidate_id.strip())
        if result:
            print(f"[CANDIDATE LOGIN] Success: status={result.get('status', 'pending')}")
            return result
        print(f"[CANDIDATE LOGIN] FAILED: authenticate_candidate returned None")
        raise HTTPException(status_code=401, detail="Invalid candidate credentials")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[CANDIDATE LOGIN] EXCEPTION: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Login error: {type(e).__name__}: {str(e)[:200]}")


class CandidateLinkRequest(BaseModel):
    candidate_id: str
    token: str


def _raise_for_link_result(result: dict) -> None:
    reason = result.get('reason', 'invalid')
    if reason == 'used':
        raise HTTPException(status_code=410, detail="This test call link has already been used")
    if reason == 'expired':
        raise HTTPException(status_code=410, detail="This test call link has expired")
    raise HTTPException(status_code=404, detail="Invalid test call link")


@app.get("/api/candidate-link")
async def validate_candidate_link(candidate_id: str, token: str) -> dict:
    """Validate an auto-generated, schedule-based test-call link (candidate_id +
    token query params, from a candidate's own Candidates row). Public endpoint —
    used by the landing page before login, distinct from the manual "Generate
    Link" (Attempts-style) flow."""
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    result = sheets.validate_candidate_link(candidate_id, token)
    if not result.get('ok'):
        _raise_for_link_result(result)
    candidate = result['candidate']
    return {
        "candidate_name": candidate.get('candidate_name', ''),
        "candidate_email": candidate.get('candidate_email', ''),
    }


@app.post("/api/candidate-link/login")
async def candidate_link_login(req: CandidateLinkRequest) -> dict:
    """Log the candidate in via their auto-generated link (candidate_id + token) —
    no email/access-code typing required. Re-validates independently of the GET
    above so this endpoint is safe to call on its own."""
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    result = sheets.validate_candidate_link(req.candidate_id, req.token)
    if not result.get('ok'):
        _raise_for_link_result(result)
    candidate = result['candidate']
    token = auth.create_candidate_token(candidate)
    return {
        "token": token,
        "candidate": {
            "candidate_id": candidate['candidate_id'],
            "candidate_name": candidate['candidate_name'],
            "scenario": candidate['scenario'],
        },
        "status": "pending",
    }


@app.post("/api/candidate/start-call")
async def candidate_start_call(req: CandidateStartCallRequest, request: Request) -> dict:
    """Start test call for candidate.

    Requires Candidate JWT.
    Transitions status: pending → started
    Routes: Queue → Classification → random active AI Client
    If attempt_id provided (trial flow), uses the assigned AI Client from the attempt.
    """
    import random as _random

    # Extract and validate candidate from JWT
    auth_header = request.headers.get('Authorization', '')
    token = auth_header[7:] if auth_header.startswith('Bearer ') else ''
    payload = auth.user_from_token(token)

    if not payload or payload.get('user_type') != 'CANDIDATE':
        raise HTTPException(status_code=401, detail="Candidate authentication required")

    candidate_id = payload.get('candidate_id')
    if not candidate_id:
        raise HTTPException(status_code=400, detail="Invalid candidate token")

    # Get current candidate status from Google Sheets
    try:
        from sheets import get_sheets_client
        sheets = get_sheets_client()

        # Verify current status
        candidate = sheets.get_candidate(
            payload.get('email', ''),
            candidate_id
        )

        if not candidate:
            raise HTTPException(status_code=404, detail="Candidate not found")

        current_status = candidate.get('test_call_status', 'pending')

        # Check if call can be started
        if current_status not in ('pending', 'NOT_SENT', 'LINK_SENT'):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot start call: status is {current_status}"
            )

        # Determine AI Client and scenario
        queue = candidate.get('queue', '')
        scenario = candidate.get('scenario', 'new-lead-discovery-call')
        ai_client_config = {}
        attempt_id = req.attempt_id

        if attempt_id:
            # Trial/Attempt flow: use AI Client from the attempt
            attempt = sheets.get_attempt(attempt_id)
            if not attempt or attempt['status'] != 'pending':
                raise HTTPException(status_code=400, detail="Invalid or used attempt")
            if attempt['candidate_id'] != candidate_id:
                raise HTTPException(status_code=403, detail="Attempt not for this candidate")

            if attempt.get('ai_client_id'):
                ai_client = sheets.get_ai_client(attempt['ai_client_id'])
                if ai_client:
                    ai_client_config = ai_client
                    scenario = ai_client.get('scenario', scenario)
            elif attempt.get('classification_id'):
                # No specific AI Client — pick random active one from classification
                ai_clients = sheets.get_ai_clients(
                    classification_id=attempt['classification_id'],
                    active_only=True,
                )
                if ai_clients:
                    selected_client = _random.choice(ai_clients)
                    ai_client_config = selected_client
                    scenario = selected_client.get('scenario', scenario)
        elif queue:
            # Normal Candidate flow: Route Queue → Classification → random AI Client
            classifications = sheets.get_classifications()
            matching_classification = None
            for cls in classifications:
                if cls['name'].strip().lower() == queue.strip().lower():
                    matching_classification = cls
                    break

            if matching_classification:
                ai_clients = sheets.get_ai_clients(
                    classification_id=matching_classification['classification_id'],
                    active_only=True,
                )
                if ai_clients:
                    selected_client = _random.choice(ai_clients)
                    ai_client_config = selected_client
                    scenario = selected_client.get('scenario', scenario)
                    logger.info("Routed candidate %s: Queue=%s → Classification=%s → AI Client=%s",
                                candidate_id, queue, matching_classification['name'], selected_client['name'])

        # Update status to started (with timestamp)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        sheets.update_candidate_status(candidate_id, 'started', timestamp)

        # Generate LiveKit token
        room = f"{scenario}_{candidate_id}"
        identity = f"sdr_{uuid.uuid4().hex[:4]}"

        metadata = json.dumps({
            "scenario": scenario,
            "username": candidate.get('candidate_name', ''),
            "industry": "",
            "candidate_id": candidate_id,
            "candidate_name": candidate.get('candidate_name', ''),
            "candidate_email": candidate.get('candidate_email', ''),
            "user_type": "CANDIDATE",
            "callback_url": os.getenv('HIRINGFLOW_WEBHOOK_URL', ''),
            "ai_client": ai_client_config,
            "call_type": "VIDEO_CALL",
            "attempt_id": attempt_id or "",
        })

        token_obj = (
            AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            .with_identity(identity)
            .with_grants(VideoGrants(room_join=True, room=room, room_record=True))
            .with_room_config(
                RoomConfiguration(
                    agents=[
                        RoomAgentDispatch(
                            agent_name=AGENT_NAME,
                            metadata=metadata,
                        )
                    ]
                )
            )
            .to_jwt()
        )

        # Register trial rooms for evaluation access control
        if attempt_id:
            _trial_rooms.add(room)
            logger.info("Trial room registered: %s (attempt=%s)", room, attempt_id)

        return {
            "url": LIVEKIT_URL,
            "token": token_obj,
            "room": room,
            "session_id": room,
            "scenario": scenario,
            "ai_client_name": ai_client_config.get('name', ''),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start call: {str(e)}")


@app.post("/api/candidate/end-call")
async def candidate_end_call(request: Request) -> dict:
    """End test call for candidate.

    Requires Candidate JWT.
    Transitions status: started → ended
    Writes TestCallEndedAt immediately.
    """
    auth_header = request.headers.get('Authorization', '')
    token = auth_header[7:] if auth_header.startswith('Bearer ') else ''
    payload = auth.user_from_token(token)

    if not payload or payload.get('user_type') != 'CANDIDATE':
        raise HTTPException(status_code=401, detail="Candidate authentication required")

    candidate_id = payload.get('candidate_id')
    if not candidate_id:
        raise HTTPException(status_code=400, detail="Invalid candidate token")

    try:
        from sheets import get_sheets_client
        sheets = get_sheets_client()

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        sheets.update_candidate_status(candidate_id, 'ended', timestamp)

        logger.info("Candidate %s end-call: status=ended, timestamp=%s", candidate_id, timestamp)
        return {"status": "ended", "timestamp": timestamp}

    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Failed to end candidate call: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to end call: {str(e)}")


@app.get("/api/candidate/status")
async def candidate_status(request: Request) -> dict:
    """Get candidate test call status.

    Requires Candidate JWT.
    """
    auth_header = request.headers.get('Authorization', '')
    token = auth_header[7:] if auth_header.startswith('Bearer ') else ''
    payload = auth.user_from_token(token)

    if not payload or payload.get('user_type') != 'CANDIDATE':
        raise HTTPException(status_code=401, detail="Candidate authentication required")

    candidate_id = payload.get('candidate_id')
    if not candidate_id:
        raise HTTPException(status_code=400, detail="Invalid candidate token")

    try:
        from sheets import get_sheets_client
        sheets = get_sheets_client()

        candidate = sheets.get_candidate(
            payload.get('email', ''),
            candidate_id
        )

        if not candidate:
            raise HTTPException(status_code=404, detail="Candidate not found")

        return {
            "candidate_id": candidate['candidate_id'],
            "candidate_name": candidate['candidate_name'],
            "scenario": candidate['scenario'],
            "status": candidate.get('test_call_status', 'pending'),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get status: {str(e)}")


@app.post("/api/candidates/{candidate_id}/regenerate")
async def regenerate_candidate(
    candidate_id: str,
    request: Request,
    body: dict
) -> dict:
    """Regenerate test call link for candidate.

    Requires ADMIN, TA_MANAGER, or TA_TEAM_LEADER role.
    """
    # Extract and validate internal user from JWT
    auth_header = request.headers.get('Authorization', '')
    token = auth_header[7:] if auth_header.startswith('Bearer ') else ''
    payload = auth.user_from_token(token)

    if not payload or payload.get('user_type') != 'INTERNAL':
        raise HTTPException(status_code=401, detail="Internal user authentication required")

    role = payload.get('role', '')
    allowed_roles = ['ADMIN', 'TA_MANAGER', 'TA_TEAM_LEADER']
    if role not in allowed_roles:
        raise HTTPException(
            status_code=403,
            detail="Insufficient permissions for regeneration"
        )

    try:
        from sheets import get_sheets_client
        sheets = get_sheets_client()

        new_candidate = sheets.increment_regeneration(candidate_id)
        if not new_candidate:
            raise HTTPException(status_code=404, detail=f"Candidate {candidate_id} not found")

        return {
            "candidate_id": new_candidate['candidate_id'],
            "access_code": new_candidate['candidate_id'],  # CandidateID IS access code
            "status": new_candidate['status'],
            "attempt_number": new_candidate['attempt_number'],
            "regeneration_count": new_candidate['regeneration_count'],
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to regenerate: {str(e)}")


@app.post("/api/logout")
async def logout(authorization: str = Header(None)) -> dict:
    return {"ok": True}


@app.get("/api/me")
async def me(user: dict = Depends(require_user)) -> dict:
    return {"user": user}


@app.post("/api/token")
async def create_token(req: TokenRequest, user: dict = Depends(require_permission("make_calls"))) -> dict:
    if not _scenario_exists(req.scenario):
        raise HTTPException(status_code=400, detail="سيناريو غير معروف")
    if not LIVEKIT_URL or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise HTTPException(status_code=500, detail="LiveKit credentials not configured in .env")

    # جلب بيانات السيناريو (بما فيها business_field)
    scenario_data = {}
    try:
        scenario_data = json.loads((SCENARIOS_DIR / f"{req.scenario}.json").read_text(encoding="utf-8"))
    except Exception:
        pass

    room = f"{req.scenario}_{secrets.token_hex(6)}"
    identity = f"sdr_{secrets.token_hex(4)}"
    metadata = json.dumps({
        "scenario": req.scenario,
        "username": user.get("name") or user.get("username", "unknown"),
        "industry": scenario_data.get("business_field", ""),
    })
    token = (
        AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_grants(VideoGrants(room_join=True, room=room))
        .with_room_config(
            RoomConfiguration(
                agents=[
                    RoomAgentDispatch(
                        agent_name=AGENT_NAME,
                        metadata=metadata,
                    )
                ]
            )
        )
        .to_jwt()
    )
    return {"url": LIVEKIT_URL, "token": token, "room": room, "scenario": req.scenario}


class AdminStartCallRequest(BaseModel):
    ai_client_id: str


@app.post("/api/admin/start-call")
async def admin_start_call(req: AdminStartCallRequest, user: dict = Depends(require_permission("make_calls"))) -> dict:
    """Start a call directly with a specific AI client (admin only)."""
    if not LIVEKIT_URL or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise HTTPException(status_code=500, detail="LiveKit credentials not configured")

    from sheets import get_sheets_client
    sheets = get_sheets_client()

    ai_client = sheets.get_ai_client(req.ai_client_id)
    if not ai_client:
        raise HTTPException(status_code=404, detail="AI client not found")

    scenario = ai_client.get('scenario', 'new-lead-discovery-call')
    room = f"admin_{secrets.token_hex(6)}"
    identity = f"sdr_{secrets.token_hex(4)}"
    call_id = f"CALL-{uuid.uuid4().hex[:10].upper()}"

    metadata = json.dumps({
        "scenario": scenario,
        "username": user.get("name") or user.get("username", "admin"),
        "industry": ai_client.get('business_field', ''),
        "user_type": "ADMIN",
        "ai_client": ai_client,
        "call_type": "VIDEO_CALL",
        "call_id": call_id,
    })

    token_obj = (
        AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_grants(VideoGrants(room_join=True, room=room, room_record=True))
        .with_room_config(
            RoomConfiguration(
                agents=[
                    RoomAgentDispatch(
                        agent_name=AGENT_NAME,
                        metadata=metadata,
                    )
                ]
            )
        )
        .to_jwt()
    )

    _trial_rooms.add(room)

    # Log call to Calls sheet
    try:
        sheets.log_call(
            room=room,
            ai_client_id=req.ai_client_id,
            ai_client_name=ai_client.get('name', ''),
            classification_id=ai_client.get('classification_id', ''),
            caller_name=user.get("name") or user.get("username", "admin"),
            call_id=call_id,
        )
    except Exception as e:
        logger.warning("Failed to log call: %s", e)

    return {
        "url": LIVEKIT_URL,
        "token": token_obj,
        "room": room,
        "scenario": scenario,
        "ai_client_name": ai_client.get('name', ''),
    }


@app.post("/api/start-session")
async def start_session(req: StartSessionRequest, _auth: bool = Depends(require_hiringflow_api_key)) -> dict:
    """Start a test session for a HiringFlow candidate.

    Generates a LiveKit token and returns it for the candidate to join the call.
    Requires Authorization: Bearer {HIRINGFLOW_API_KEY} header.
    """
    if not LIVEKIT_URL or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise HTTPException(status_code=500, detail="LiveKit credentials not configured")

    # Determine scenario: use provided scenario_id or default based on queue
    scenario_id = req.scenario_id
    if not scenario_id:
        scenario_id = "new-lead-discovery-call" if req.queue == "SDR" else "lead-follow-up-call"

    if not _scenario_exists(scenario_id):
        # Try fallback to first available scenario
        available = list(personas.SCENARIOS.keys())
        if available:
            scenario_id = available[0]
        else:
            raise HTTPException(status_code=400, detail=f"Scenario not found: {req.scenario_id}")

    # Get scenario data
    scenario_data = {}
    try:
        scenario_data = json.loads((SCENARIOS_DIR / f"{scenario_id}.json").read_text(encoding="utf-8"))
    except Exception:
        pass

    # Generate room and token
    room = f"{scenario_id}_{secrets.token_hex(6)}"
    identity = f"sdr_{secrets.token_hex(4)}"
    metadata = json.dumps({
        "scenario": scenario_id,
        "username": req.candidate_name,
        "industry": scenario_data.get("business_field", ""),
        "test_session_id": req.test_session_id,
    })

    token = (
        AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_grants(VideoGrants(room_join=True, room=room, room_record=True))
        .with_room_config(
            RoomConfiguration(
                agents=[
                    RoomAgentDispatch(
                        agent_name=AGENT_NAME,
                        metadata=metadata,
                    )
                ]
            )
        )
        .to_jwt()
    )

    scenario_summary = {}
    try:
        all_scenarios = _all_scenario_summaries()
        for s in all_scenarios:
            if s.get("id") == scenario_id:
                scenario_summary = s
                break
    except Exception:
        pass

    return {
        "test_session_id": req.test_session_id,
        "livekit_url": LIVEKIT_URL,
        "livekit_token": token,
        "room": room,
        "scenario": scenario_summary or {"id": scenario_id, "name": scenario_id},
    }


@app.get("/api/scenarios")
async def list_scenarios(user: dict = Depends(require_user)) -> dict:
    return {"scenarios": _all_scenario_summaries()}


@app.post("/api/random-call")
async def random_call(user: dict = Depends(require_permission("make_calls"))) -> dict:
    scenarios = _all_scenario_summaries()
    if not scenarios:
        raise HTTPException(status_code=404, detail="لا توجد سيناريوهات")
    return {"scenario": random.choice(scenarios)}


@app.post("/api/scenarios/custom")
async def create_custom_scenario(req: CustomScenarioRequest, admin: dict = Depends(require_permission("make_calls"))) -> dict:
    if not req.customer_name.strip() or not req.business_field.strip():
        raise HTTPException(status_code=400, detail="اسم العميل ومجال العمل مطلوبان")

    sid = f"custom_{secrets.token_hex(6)}"
    brief = req.model_dump()
    brief["id"] = sid
    brief["name"] = req.name.strip() or f"عميل مخصص – {req.customer_name.strip()}"
    _custom_briefs[sid] = brief
    (SCENARIOS_DIR / f"{sid}.json").write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")

    scenario = personas.build_custom_scenario(brief)
    return {"scenario": personas.custom_scenario_summary(scenario)}


@app.post("/api/upload-call")
async def upload_call(file: UploadFile = File(...), admin: dict = Depends(require_permission("manage_ai_clients"))) -> dict:
    """يرفع مكالمة مسجلة (MP3…) ويتعلم منها عميلًا ذكيًا جديدًا + يحسّن الأساس العامي."""
    filename = file.filename or "call"
    ext = Path(filename).suffix.lower()
    if ext not in call_learning.CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail="الصيغة غير مدعومة: أرسل MP3 أو M4A أو WAV أو WEBM أو OGG",
        )

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY غير مضبوطة في .env لتحليل المكالمة")

    CALLS_DIR.mkdir(parents=True, exist_ok=True)
    save_path = CALLS_DIR / f"{int(time.time())}_{secrets.token_hex(3)}{ext}"
    save_path.write_bytes(await file.read())

    try:
        utterances = await call_learning.transcribe_audio(save_path)
        if not utterances:
            raise HTTPException(
                status_code=422,
                detail="لم يُستخرج نص من المكالمة — تأكد أن الملف يحتوي كلامًا واضحًا وصوته مسموعًا",
            )
        gemini_model = os.getenv("GEMINI_LLM_MODEL", "gemini-flash-lite-latest")
        profile = await call_learning.analyze_transcript(utterances, gemini_key, gemini_model)
        if not profile:
            raise HTTPException(status_code=500, detail="تعذّر تحليل محتوى المكالمة")

        brief = call_learning.profile_to_brief(profile)
        sid = f"custom_{secrets.token_hex(6)}"
        brief["id"] = sid
        _custom_briefs[sid] = brief
        (SCENARIOS_DIR / f"{sid}.json").write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")

        added = call_learning.add_to_corpus(brief.get("dialect_phrases") or [])

        scenario = personas.build_custom_scenario(brief)
        return {
            "scenario": personas.custom_scenario_summary(scenario),
            "learned_phrases": added,
            "transcript_words": sum(len(u["text"].split()) for u in utterances),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"تعذّر معالجة المكالمة: {exc}")


@app.get("/api/scenarios/{scenario_id}")
async def get_scenario(scenario_id: str, user: dict = Depends(require_user_or_service)) -> dict:
    """يستخدمه العميل الآلي لجلب ملف العميل المخصص (custom_*)."""
    if not scenario_id.startswith("custom_"):
        raise HTTPException(status_code=404, detail="Not found")
    brief = _custom_briefs.get(scenario_id)
    if brief is None:
        raise HTTPException(status_code=404, detail="سيناريو غير معروف")
    return {"brief": brief}


@app.post("/api/results")
async def save_result(request: Request) -> dict:
    if WEBHOOK_SECRET:
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {WEBHOOK_SECRET}":
            raise HTTPException(status_code=401, detail="Unauthorized")
    else:
        # No webhook secret configured — reject all external submissions
        raise HTTPException(status_code=403, detail="Result submission not configured")
    body = await request.json()
    room = body.get("room")
    result = body.get("result")
    if not room or not result:
        raise HTTPException(status_code=400, detail="room and result are required")
    _results[room] = result
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / f"{room}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


@app.get("/api/results/{room}")
async def get_result(room: str, user: dict = Depends(require_user)) -> dict:
    # Authorization: Candidates cannot access evaluation data
    # Exception: trial/attempt-based rooms (evaluation visible to trial users)
    if user.get("user_type") == "CANDIDATE":
        if room not in _trial_rooms:
            raise HTTPException(status_code=403, detail="Candidates cannot access evaluation data")

    if room in _results:
        return {"status": "ready", "result": _results[room]}
    result_file = DATA_DIR / f"{room}.json"
    if result_file.exists():
        try:
            result = json.loads(result_file.read_text(encoding="utf-8"))
            _results[room] = result
            return {"status": "ready", "result": result}
        except Exception:
            pass
    return {"status": "pending"}


@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/diagnostic")
async def diagnostic(user: dict = Depends(require_permission("manage_users"))) -> dict:
    """Local-only diagnostic endpoint. Reports configuration status without exposing secrets."""
    import os.path
    result = {
        "google_sheets_configured": bool(os.getenv("GOOGLE_SHEET_ID")),
        "google_credentials_loaded": False,
        "google_sheet_accessible": False,
        "heads_tab_found": False,
        "candidates_tab_found": False,
        "jwt_secret_configured": bool(os.getenv("JWT_SECRET")),
    }
    try:
        creds_path = os.getenv("GOOGLE_CREDENTIALS", "")
        result["google_credentials_loaded"] = os.path.exists(creds_path)
    except Exception:
        pass
    try:
        from sheets import get_sheets_client
        client = get_sheets_client()
        result["google_sheet_accessible"] = True
        tabs = [ws.title for ws in client.sheet.worksheets()]
        result["heads_tab_found"] = "Heads" in tabs
        result["candidates_tab_found"] = "Candidates" in tabs
    except Exception as e:
        result["google_sheets_error"] = str(e)[:200]
    return result


@app.get("/api/get-webhook-url")
async def get_webhook_url(user: dict = Depends(require_permission("manage_users"))) -> dict:
    """Return the HiringFlow webhook URL from environment or Apps Script. Admin only."""
    url = os.getenv("HIRINGFLOW_WEBHOOK_URL", "")
    if not url:
        apps_script_url = os.getenv("HIRINGFLOW_APPSCRIPT_URL", "")
        if apps_script_url:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(apps_script_url, params={"action": "getWebhookUrl"})
                    data = resp.json()
                    url = data.get("webhook_url", "")
            except Exception:
                pass
    return {"webhook_url": url}


# =============================================================================
# Test Session Management
# =============================================================================

@app.get("/api/test-sessions")
async def list_test_sessions(
    classification_id: str = None,
    user: dict = Depends(require_permission("view_results")),
) -> dict:
    """List test sessions (attempts) with optional classification filter."""
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    scoped_ids = scoped_classification_ids(user)
    attempts = sheets.get_attempts(classification_id=classification_id) if hasattr(sheets, 'get_attempts') else []
    attempts = [a for a in attempts if in_classification_scope(a.get("classification_id", ""), scoped_ids)]
    return {"test_sessions": attempts}


@app.get("/api/test-sessions/{attempt_id}")
async def get_test_session(attempt_id: str, user: dict = Depends(require_permission("view_results"))) -> dict:
    """Get test session details."""
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    attempt = sheets.get_attempt(attempt_id)
    if not attempt:
        raise HTTPException(status_code=404, detail="Test session not found")
    return attempt


@app.post("/api/test-sessions/{attempt_id}/revoke")
async def revoke_test_session(attempt_id: str, admin: dict = Depends(require_permission("generate_test_links"))) -> dict:
    """Revoke an unused test session link."""
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    if hasattr(sheets, 'revoke_attempt'):
        revoked = sheets.revoke_attempt(attempt_id)
        if not revoked:
            raise HTTPException(status_code=404, detail="Test session not found or already used")
        return {"ok": True}
    raise HTTPException(status_code=501, detail="Revoke not implemented")


class LinkSettingsRequest(BaseModel):
    max_reschedule_count: int


@app.get("/api/link-settings")
async def get_link_settings(user: dict = Depends(require_permission("generate_test_links"))) -> dict:
    """Get admin-configurable settings for the test-call link system."""
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    return {"max_reschedule_count": sheets.get_max_reschedule_count()}


@app.post("/api/link-settings")
async def update_link_settings(req: LinkSettingsRequest, admin: dict = Depends(require_permission("generate_test_links"))) -> dict:
    """Update the max number of reschedules a candidate may use and still get an
    auto-generated test-call link (see agent.py's test-call-link poller)."""
    if req.max_reschedule_count < -1:
        raise HTTPException(status_code=400, detail="max_reschedule_count must be -1 (unlimited) or >= 0")
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    ok = sheets.set_link_setting('MaxRescheduleCount', str(req.max_reschedule_count))
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to save link settings")
    return {"ok": True, "max_reschedule_count": req.max_reschedule_count}


@app.get("/api/results")
async def list_results(
    classification_id: str = None,
    user: dict = Depends(require_permission("view_results")),
) -> dict:
    """List all evaluation results."""
    scoped_ids = scoped_classification_ids(user)
    results_list = []
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for f in DATA_DIR.glob("*.json"):
        try:
            result = json.loads(f.read_text(encoding="utf-8"))
            if not in_classification_scope(result.get("classification_id", ""), scoped_ids):
                continue
            result["_room"] = f.stem
            results_list.append(result)
        except Exception:
            pass
    return {"results": results_list}


@app.get("/api/results/{room}/pdf")
async def download_result_pdf(room: str, user: dict = Depends(require_permission("view_recordings"))):
    """Download evaluation PDF for a room."""
    from fastapi.responses import FileResponse
    import os
    
    # First: try to get PDF path from stored result
    result_file = DATA_DIR / f"{room}.json"
    if result_file.exists():
        try:
            result_data = json.loads(result_file.read_text(encoding="utf-8"))
            pdf_path = result_data.get("_pdf_path")
            if pdf_path and os.path.exists(pdf_path):
                return FileResponse(
                    path=pdf_path,
                    media_type="application/pdf",
                    filename=f"evaluation_{room}.pdf",
                )
        except Exception:
            pass
    
    # Fallback: search for PDF in recordings directory matching the room
    recordings_path = os.getenv("RECORDINGS_PATH", os.path.join(BASE_DIR, "recordings"))
    for root, dirs, files in os.walk(recordings_path):
        if "evaluation.pdf" in files:
            # Match by folder name containing the room key
            folder = os.path.basename(root)
            if room in folder or folder in room:
                pdf_path = os.path.join(root, "evaluation.pdf")
                return FileResponse(
                    path=pdf_path,
                    media_type="application/pdf",
                    filename=f"evaluation_{room}.pdf",
                )
    
    # Last resort: return any PDF found
    for root, dirs, files in os.walk(recordings_path):
        if "evaluation.pdf" in files:
            pdf_path = os.path.join(root, "evaluation.pdf")
            return FileResponse(
                path=pdf_path,
                media_type="application/pdf",
                filename=f"evaluation_{room}.pdf",
            )
    
    raise HTTPException(status_code=404, detail="PDF not found. Make a call first to generate evaluation.")


def _delete_call_data(room: str) -> dict:
    """يمسح كل ما يخص مكالمة واحدة: نتيجة JSON، مجلد التسجيل (صوت/PDF/نص)، وصف Calls
    في الشيت. لا يفشل بالكامل لو مصدر واحد مفقود — يرجع ملخص بكل ما تم مسحه فعليًا."""
    deleted = {"result_json": False, "recording_folder": False, "sheet_row": False}

    result_file = DATA_DIR / f"{room}.json"
    if result_file.exists():
        try:
            result_data = json.loads(result_file.read_text(encoding="utf-8"))
            pdf_path = result_data.get("_pdf_path")
            if pdf_path:
                call_dir = os.path.dirname(pdf_path)
                if call_dir and os.path.isdir(call_dir):
                    shutil.rmtree(call_dir, ignore_errors=True)
                    deleted["recording_folder"] = True
        except Exception as e:
            logger.warning("Could not resolve recording folder for room %s: %s", room, e)
        try:
            result_file.unlink()
            deleted["result_json"] = True
        except Exception as e:
            logger.warning("Could not delete result json for room %s: %s", room, e)

    try:
        from sheets import get_sheets_client
        deleted["sheet_row"] = get_sheets_client().delete_call(room)
    except Exception as e:
        logger.warning("Could not delete sheet row for room %s: %s", room, e)

    return deleted


@app.delete("/api/results/{room}")
async def delete_result(room: str, admin: dict = Depends(require_permission("delete_calls_results"))) -> dict:
    """يمسح مكالمة واحدة نهائيًا من كل أماكن التخزين (JSON، التسجيل، صف الشيت)."""
    deleted = _delete_call_data(room)
    if not any(deleted.values()):
        raise HTTPException(status_code=404, detail="Call not found")
    return {"ok": True, "room": room, "deleted": deleted}


class BulkDeleteResultsRequest(BaseModel):
    rooms: list[str]


@app.post("/api/results/bulk-delete")
async def bulk_delete_results(payload: BulkDeleteResultsRequest, admin: dict = Depends(require_permission("delete_calls_results"))) -> dict:
    """يمسح عدة مكالمات دفعة واحدة (تحديد متعدد أو تحديد الكل من صفحة النتائج)."""
    results = []
    for room in payload.rooms:
        deleted = _delete_call_data(room)
        results.append({"room": room, "deleted": deleted, "found": any(deleted.values())})
    return {"results": results}


# =============================================================================
# Classifications CRUD
# =============================================================================

class ClassificationRequest(BaseModel):
    name: str
    description: str = ""
    min_duration_minutes: str = ""
    max_duration_minutes: str = ""
    default_difficulty: str = "random"
    default_personality: str = "random"
    default_knowledgeable: str = "random"


@app.get("/api/classifications")
async def list_classifications(user: dict = Depends(require_user)) -> dict:
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    return {"classifications": sheets.get_classifications()}


@app.post("/api/classifications")
async def create_classification(req: ClassificationRequest, admin: dict = Depends(require_permission("manage_classifications"))) -> dict:
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    result = sheets.create_classification(
        req.name.strip(), req.description.strip(),
        req.min_duration_minutes.strip(), req.max_duration_minutes.strip(),
        req.default_difficulty.strip(), req.default_personality.strip(), req.default_knowledgeable.strip(),
    )
    if not result:
        raise HTTPException(status_code=500, detail="Failed to create classification")
    return result


@app.put("/api/classifications/{classification_id}")
async def update_classification(
    classification_id: str,
    req: ClassificationRequest,
    admin: dict = Depends(require_permission("manage_classifications")),
) -> dict:
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    updated = sheets.update_classification(
        classification_id, req.name.strip(), req.description.strip(),
        req.min_duration_minutes.strip(), req.max_duration_minutes.strip(),
        req.default_difficulty.strip(), req.default_personality.strip(), req.default_knowledgeable.strip(),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Classification not found")
    return {"ok": True}


@app.delete("/api/classifications/{classification_id}")
async def delete_classification(classification_id: str, admin: dict = Depends(require_permission("manage_classifications"))) -> dict:
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    deleted = sheets.delete_classification(classification_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Classification not found")
    return {"ok": True}


@app.post("/api/classifications/{classification_id}/reset-ai-clients")
async def reset_classification_ai_clients(
    classification_id: str,
    admin: dict = Depends(require_permission("manage_classifications")),
) -> dict:
    """Bulk-overwrite every AI Client in this classification's Difficulty,
    Personality and Knowledgeable back to the classification's own current
    default settings — undoes any per-client overrides made afterward."""
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    if not sheets.get_classification(classification_id):
        raise HTTPException(status_code=404, detail="Classification not found")
    count = sheets.reset_ai_clients_to_classification_defaults(classification_id)
    return {"ok": True, "updated_count": count}


# =============================================================================
# AI Clients CRUD
# =============================================================================

class AIClientRequest(BaseModel):
    name: str = ""
    classification_id: str = ""
    queue: str = ""  # one of sheets.GoogleSheetsClient.QUEUE_OPTIONS
    scenario: str = "new-lead-discovery-call"
    dialect: str = "saudi"  # ignored on write — always derived server-side from country
    customer_language: str = "ar"  # "ar" or "en"
    difficulty: str = "medium"
    personality: str = ""
    instructions: str = ""
    objectives: str = ""
    objections: str = ""
    customer_name: str = ""
    customer_role: str = ""
    company_name: str = ""
    business_field: str = ""
    company_size: str = ""
    pain_point: str = ""
    decision_maker: bool = True
    budget_sensitivity: str = "متوسطة"
    buying_intent: str = "متوسطة"
    temperature: str = "warm"
    product_brief: str = ""
    voice: str = ""
    knowledgeable: str = "false"  # "true" / "false" / "random" — resolved per-call if "random"
    active: bool = True
    country: str = ""  # "sa" أو "eg" — يحدد أي رابط أسعار دفترة يُستخدم للتقييم


# =============================================================================
# Attempts (One-time Test Call Links)
# =============================================================================

class AttemptRequest(BaseModel):
    candidate_id: str
    candidate_email: str
    candidate_name: str
    classification_id: str = ""
    ai_client_id: str = ""


@app.post("/api/attempts")
async def create_attempt(req: AttemptRequest, admin: dict = Depends(require_permission("generate_test_links"))) -> dict:
    """Generate a one-time test call link for a candidate."""
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    attempt = sheets.create_attempt(
        req.candidate_id, req.candidate_email, req.candidate_name,
        req.classification_id, req.ai_client_id,
    )
    if not attempt:
        raise HTTPException(status_code=500, detail="Failed to create attempt")
    return attempt


@app.get("/api/attempts/{attempt_id}")
async def get_attempt(attempt_id: str, token: str = "") -> dict:
    """Validate and get attempt info. Public endpoint for trial links.

    Requires both attempt_id and access_token for security.
    """
    from sheets import get_sheets_client
    sheets = get_sheets_client()

    if token:
        # Validate by access token (secure — used in trial links)
        attempt = sheets.get_attempt_by_token(token)
        if not attempt:
            raise HTTPException(status_code=404, detail="Invalid or expired test call link")
        if attempt['attempt_id'] != attempt_id:
            raise HTTPException(status_code=403, detail="Link mismatch")
    else:
        # Fallback: validate by attempt_id only (for admin/internal use)
        attempt = sheets.get_attempt(attempt_id)
        if not attempt:
            raise HTTPException(status_code=404, detail="Invalid or expired test call link")

    if attempt['status'] != 'pending':
        raise HTTPException(status_code=410, detail="This test call link has already been used")
    return attempt


@app.post("/api/attempts/{attempt_id}/consume")
async def consume_attempt(attempt_id: str, request: Request) -> dict:
    """Consume a one-time test call link (called when candidate starts call)."""
    # Extract and validate candidate from JWT
    auth_header = request.headers.get('Authorization', '')
    token = auth_header[7:] if auth_header.startswith('Bearer ') else ''
    payload = auth.user_from_token(token)

    if not payload or payload.get('user_type') != 'CANDIDATE':
        raise HTTPException(status_code=401, detail="Candidate authentication required")

    from sheets import get_sheets_client
    sheets = get_sheets_client()

    attempt = sheets.get_attempt(attempt_id)
    if not attempt:
        raise HTTPException(status_code=404, detail="Invalid attempt")
    if attempt['status'] != 'pending':
        raise HTTPException(status_code=410, detail="Link already used")

    # Verify candidate matches attempt
    if attempt['candidate_id'] != payload.get('candidate_id'):
        raise HTTPException(status_code=403, detail="This link is not for this candidate")

    consumed = sheets.consume_attempt(attempt_id)
    if not consumed:
        raise HTTPException(status_code=410, detail="Link already used")

    return {"ok": True, "attempt_id": attempt_id}


@app.get("/api/calls")
async def list_calls(
    classification_id: str = None,
    user: dict = Depends(require_permission("view_results")),
) -> dict:
    """List all calls made with AI clients."""
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    scoped_ids = scoped_classification_ids(user)
    calls = sheets.get_calls(classification_id)
    calls = [c for c in calls if in_classification_scope(c.get("classification_id", ""), scoped_ids)]
    return {"calls": calls}


@app.get("/api/ai-clients")
async def list_ai_clients(
    classification_id: str = None,
    active_only: bool = False,
    user: dict = Depends(require_user),
) -> dict:
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    scoped_ids = scoped_classification_ids(user)
    clients = sheets.get_ai_clients(classification_id, active_only)
    clients = [c for c in clients if in_classification_scope(c.get("classification_id", ""), scoped_ids)]
    return {"ai_clients": clients}


@app.post("/api/ai-clients")
async def create_ai_client(req: AIClientRequest, admin: dict = Depends(require_permission("manage_ai_clients"))) -> dict:
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    result = sheets.create_ai_client(req.model_dump())
    if not result:
        raise HTTPException(status_code=500, detail="Failed to create AI client")
    return result


class GenerateAIClientsRequest(BaseModel):
    count: int
    classification_id: str = ""
    country: str = "both"  # "eg", "sa", or "both"


@app.post("/api/ai-clients/generate")
async def generate_ai_clients(req: GenerateAIClientsRequest, admin: dict = Depends(require_permission("manage_ai_clients"))) -> dict:
    """يولّد N عملاء ذكيين مختلفين تمامًا عن بعض بالاستعانة بقاعدة معرفة دفترة
    (استخدام واحد فقط لملف المعرفة الكامل — لا علاقة له بتكلفة المكالمات الحية).
    الصعوبة والشخصية بتتحدد دايمًا لاحقًا وقت المكالمة (قيمة "random")، مش هنا.
    الدولة تحدد اللهجة تلقائيًا (مفيش اختيار لهجة مستقل)، ويتم تجاهل أي عميل
    مولّد بنفس اسم شركة/عميل موجود بالفعل لتجنّب التكرار."""
    if req.count < 1 or req.count > 50:
        raise HTTPException(status_code=400, detail="العدد لازم يكون بين 1 و50")

    country_pref = (req.country or "both").strip().lower()
    if country_pref not in ("eg", "sa", "both"):
        raise HTTPException(status_code=400, detail='country لازم يكون "eg" أو "sa" أو "both"')

    gemini_api_key = os.getenv("GEMINI_API_KEY", "")
    if not gemini_api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY غير مضبوط في .env")

    try:
        knowledge_text = DAFTRA_KNOWLEDGE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        knowledge_text = ""

    if country_pref == "eg":
        country_instruction = 'كل العملاء المولّدين بدون استثناء يجب أن يكونوا من مصر — اجعل "country": "eg" لكل عميل.'
    elif country_pref == "sa":
        country_instruction = 'كل العملاء المولّدين بدون استثناء يجب أن يكونوا من السعودية — اجعل "country": "sa" لكل عميل.'
    else:
        country_instruction = 'وزّع العملاء المولّدين بالتساوي تقريبًا بين مصر ("country": "eg") والسعودية ("country": "sa").'

    system_prompt = (
        "أنت خبير في تصميم شخصيات عملاء وهميين (Personas) لتدريب مندوبي مبيعات دفترة على مكالمات المبيعات. "
        f"ولّد بالضبط {req.count} عميل مختلفين تمامًا عن بعضهم البعض — في نوع النشاط، حجم الشركة، المشكلة "
        "الأساسية، ودرجة الاهتمام والميزانية. استخدم قاعدة المعرفة عن دفترة بالأسفل عشان تختار أنواع أنشطة "
        "واقعية ومواقف فعلاً معقدة تختبر مهارة المندوب (زي فروع بأرقام ضريبية مختلفة، لبس بين عدد الموظفين "
        "وعدد المستخدمين، تكامل مع نظام خارجي، منتجات تمر بمراحل تصنيع، إلخ) — وزّع هذه المواقف على العملاء "
        "المختلفين بدل ما تكررها كلها في عميل واحد. ممنوع تمامًا تختار قيمة لحقلي difficulty أو personality "
        f"أو تذكرهما في الرد — هيتحددوا عشوائيًا في الكود. {country_instruction} "
        "كل عميل لازم يكون له اسم شركة (company_name) واسم عميل (customer_name) فريدين تمامًا عن باقي العملاء "
        "في نفس الرد — ممنوع تكرار نفس اسم الشركة أو العميل مرتين.\n\n"
        "قاعدة المعرفة عن دفترة:\n" + knowledge_text
    )
    user_prompt = (
        "أعد JSON صارم بالشكل التالي فقط (بدون أي نص أو شرح إضافي)، بعدد "
        f"{req.count} عنصر بالضبط داخل المصفوفة:\n"
        '{"clients": [{"name": "اسم البروفايل، مثال: أحمد - مطعم بدر", '
        '"customer_name": "...", "customer_role": "...", "company_name": "...", '
        '"business_field": "...", "company_size": "...", "pain_point": "...", '
        '"objections": "...", "instructions": "...", "objectives": "...", '
        '"product_brief": "...", "decision_maker": true, '
        '"budget_sensitivity": "منخفضة أو متوسطة أو عالية", '
        '"buying_intent": "منخفضة أو متوسطة أو عالية", "temperature": "warm أو cold", '
        '"knowledgeable": false, "country": "sa أو eg"}, ...]}'
    )

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=gemini_api_key, base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
    try:
        resp = await client.chat.completions.create(
            model=os.getenv("GEMINI_LLM_MODEL", "gemini-flash-lite-latest"),
            response_format={"type": "json_object"},
            max_completion_tokens=min(req.count * 350 + 800, 20000),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"فشل توليد العملاء: {e}")
    finally:
        await client.close()

    try:
        data = json.loads(resp.choices[0].message.content or "{}")
        generated = data.get("clients", [])
    except Exception:
        raise HTTPException(status_code=502, detail="رد غير صالح من نموذج التوليد")

    if not generated:
        raise HTTPException(status_code=502, detail="لم يتم توليد أي عملاء")

    from sheets import get_sheets_client
    sheets = get_sheets_client()

    default_country = "sa" if country_pref == "sa" else "eg"
    # Dedup against existing clients (by company name, falling back to customer
    # name) so "Generate" never creates a client that already exists.
    existing_keys = {
        (c.get('company_name') or c.get('customer_name') or '').strip().lower()
        for c in sheets.get_ai_clients()
        if (c.get('company_name') or c.get('customer_name'))
    }
    existing_keys.discard('')

    created = []
    skipped_duplicates = 0
    for c in generated[:req.count]:
        if not isinstance(c, dict):
            continue
        dedup_key = (c.get("company_name") or c.get("customer_name") or "").strip().lower()
        if dedup_key and dedup_key in existing_keys:
            skipped_duplicates += 1
            continue
        if dedup_key:
            existing_keys.add(dedup_key)
        country = c.get("country") if c.get("country") in ("sa", "eg") else default_country
        if country_pref in ("sa", "eg"):
            country = country_pref  # enforce the requested country regardless of what the LLM returned
        payload = {
            "name": c.get("name") or c.get("customer_name") or "عميل جديد",
            "classification_id": req.classification_id,
            "scenario": "new-lead-discovery-call",
            "country": country,
            "customer_language": "ar",
            "difficulty": "random",
            "personality": "random",
            "instructions": c.get("instructions", ""),
            "objectives": c.get("objectives", ""),
            "objections": c.get("objections", ""),
            "customer_name": c.get("customer_name", ""),
            "customer_role": c.get("customer_role", ""),
            "company_name": c.get("company_name", ""),
            "business_field": c.get("business_field", ""),
            "company_size": c.get("company_size", ""),
            "pain_point": c.get("pain_point", ""),
            "decision_maker": bool(c.get("decision_maker", True)),
            "budget_sensitivity": c.get("budget_sensitivity") or "متوسطة",
            "buying_intent": c.get("buying_intent") or "متوسطة",
            "temperature": c.get("temperature") if c.get("temperature") in ("warm", "cold") else "warm",
            "product_brief": c.get("product_brief", ""),
            "voice": "",
            "knowledgeable": bool(c.get("knowledgeable", False)),
            "active": True,
        }
        result = sheets.create_ai_client(payload)
        if result:
            created.append(result)

    return {
        "created_count": len(created),
        "requested_count": req.count,
        "skipped_duplicates": skipped_duplicates,
        "clients": created,
    }


@app.put("/api/ai-clients/{client_id}")
async def update_ai_client(
    client_id: str,
    req: AIClientRequest,
    admin: dict = Depends(require_permission("manage_ai_clients")),
) -> dict:
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    updated = sheets.update_ai_client(client_id, req.model_dump(exclude_unset=True))
    if not updated:
        raise HTTPException(status_code=404, detail="AI client not found")
    return {"ok": True}


@app.delete("/api/ai-clients/{client_id}")
async def delete_ai_client(client_id: str, admin: dict = Depends(require_permission("manage_ai_clients"))) -> dict:
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    deleted = sheets.delete_ai_client(client_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="AI client not found")
    return {"ok": True}


# =============================================================================
# Daftra Knowledge Base (نص عام يُحقن في شخصية العميل الذكي وتقييم المندوب)
# =============================================================================

class KnowledgeBaseRequest(BaseModel):
    content: str


@app.get("/api/knowledge-base")
async def get_knowledge_base(admin: dict = Depends(require_permission("manage_ai_clients"))) -> dict:
    try:
        content = DAFTRA_KNOWLEDGE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    return {"content": content}


@app.post("/api/knowledge-base")
async def update_knowledge_base(req: KnowledgeBaseRequest, admin: dict = Depends(require_permission("manage_ai_clients"))) -> dict:
    DAFTRA_KNOWLEDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DAFTRA_KNOWLEDGE_PATH.write_text(req.content, encoding="utf-8")
    return {"ok": True}


# =============================================================================
# User Management (Admin only — Heads sheet)
# =============================================================================

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserRequest(BaseModel):
    username: str
    name: str
    role: str = ""
    password: str = ""
    permissions: dict[str, bool] = {}
    all_classifications: bool = False
    classification_ids: list[str] = []


class UserUpdateRequest(BaseModel):
    username: str = ""  # فاضي = من غير تغيير للبريد/اسم المستخدم
    name: str = ""
    role: str = ""
    password: str = ""  # فاضي = من غير تغيير لكلمة المرور
    status: str = ""
    permissions: dict[str, bool] = {}
    all_classifications: bool = False
    classification_ids: list[str] = []


class BulkUsersRequest(BaseModel):
    names: list[str]
    base_username: str = ""
    role: str = "sdr"


@app.get("/api/user-meta")
async def get_user_meta(user: dict = Depends(require_permission("manage_users"))) -> dict:
    """المسميات الوظيفية ومفاتيح الصلاحيات المتاحة — مصدرها auth.py، تُستخدم لبناء
    فورم المستخدم في الواجهة (بدل ما تتكرر يدويًا في الفرونت)."""
    return {"roles": auth.USER_ROLES, "permission_keys": auth.PERMISSION_KEYS}


@app.get("/api/users")
async def list_users(admin: dict = Depends(require_permission("manage_users"))) -> dict:
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    users = sheets.get_all_heads_users()
    return {"users": users}


@app.post("/api/users")
async def create_user(req: UserRequest, admin: dict = Depends(require_permission("manage_users"))) -> dict:
    if not EMAIL_RE.match(req.username):
        raise HTTPException(status_code=400, detail="اسم المستخدم لازم يكون بريد إلكتروني صحيح")
    if req.role and req.role not in auth.USER_ROLES:
        raise HTTPException(status_code=400, detail="مسمى وظيفي غير معروف")
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    import secrets as _secrets
    password = req.password or _secrets.token_urlsafe(8)
    result = sheets.create_heads_user(
        req.username, req.name, req.role, password,
        permissions=auth.normalize_permissions(req.permissions),
        all_classifications=req.all_classifications,
        classification_ids=req.classification_ids,
    )
    if not result:
        raise HTTPException(status_code=400, detail="User already exists or creation failed")
    return {"ok": True, "user": result, "password": password}


@app.put("/api/users/{username}")
async def update_user(username: str, req: UserUpdateRequest, admin: dict = Depends(require_permission("manage_users"))) -> dict:
    if req.username and not EMAIL_RE.match(req.username):
        raise HTTPException(status_code=400, detail="اسم المستخدم لازم يكون بريد إلكتروني صحيح")
    if req.role and req.role not in auth.USER_ROLES:
        raise HTTPException(status_code=400, detail="مسمى وظيفي غير معروف")
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    updated = sheets.update_heads_user(username, {
        "name": req.name,
        "email": req.username,
        "role": req.role,
        "password": req.password,
        "status": req.status,
        "permissions": auth.normalize_permissions(req.permissions) if req.permissions else None,
        "all_classifications": req.all_classifications,
        "classification_ids": req.classification_ids,
    })
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


@app.delete("/api/users/{username}")
async def delete_user(username: str, admin: dict = Depends(require_permission("manage_users"))) -> dict:
    from sheets import get_sheets_client
    sheets = get_sheets_client()
    deleted = sheets.delete_heads_user(username)
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


@app.post("/api/users/bulk")
async def bulk_users(req: BulkUsersRequest, admin: dict = Depends(require_permission("manage_users"))) -> dict:
    from sheets import get_sheets_client
    import secrets as _secrets
    sheets = get_sheets_client()
    created = []
    for i, name in enumerate(req.names):
        name = name.strip()
        if not name:
            continue
        email = f"{req.base_username}{i+1}@{name.lower().replace(' ', '')}.com" if req.base_username else f"{name.lower().replace(' ', '')}@daftra.com"
        password = _secrets.token_urlsafe(8)
        result = sheets.create_heads_user(email, name, req.role, password)
        if result:
            created.append({**result, "password": password})
    return {"ok": True, "users": created}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import asyncio
    import uvicorn

    # Fetch webhook URL from Apps Script at startup
    apps_script_url = os.getenv("HIRINGFLOW_WEBHOOK_URL", "")
    if apps_script_url:
        try:
            from hiringflow_integration import fetch_webhook_url_from_script
            asyncio.get_event_loop().run_until_complete(
                fetch_webhook_url_from_script(apps_script_url)
            )
        except Exception as e:
            print(f"[server] Could not fetch webhook URL from Apps Script: {e}")

    uvicorn.run(app, host="0.0.0.0", port=PORT)
