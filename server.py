"""الخادم الخلفي لمعمل تدريب المبيعات (SDR AI Training Lab).

يقدّم لوحة التحكم، ويصدر رموز LiveKit (مع إرسال العميل الآلي تلقائيًا)،
ويخزّن/يعيد نتائج التقييم المرسلة من العميل الآلي.

التشغيل:  python server.py
"""

import json
import os
import random
import secrets
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

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

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "").strip()
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "").strip()
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "").strip()
AGENT_NAME = os.getenv("AGENT_NAME", "sdr-training-agent")
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

app = FastAPI(title="SDR AI Training Lab")

_results: dict[str, dict] = {}
_custom_briefs: dict[str, dict] = {}

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
    pass  # No body needed - candidate identity from JWT


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


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="هذه العملية تتطلب صلاحية المدير")
    return user


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


class CandidateLoginRequest(BaseModel):
    email: str
    candidate_id: str


class CandidateStartCallRequest(BaseModel):
    pass  # No body needed - candidate identity from JWT


@app.post("/api/login")
async def login(req: LoginRequest) -> dict:
    """Internal user login using Google Sheets Heads tab."""
    print(f"[LOGIN] Attempt: username={req.username!r}")
    try:
        result = auth.authenticate_internal(req.username.strip(), req.password)
        if result:
            print(f"[LOGIN] Success: user={result['user']['name']}, role={result['user']['role']}")
            return result
        print(f"[LOGIN] FAILED: authenticate_internal returned None")
        raise HTTPException(status_code=401, detail="Invalid credentials or inactive account")
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


@app.post("/api/candidate/start-call")
async def candidate_start_call(request: Request) -> dict:
    """Start test call for candidate.

    Requires Candidate JWT.
    Transitions status: pending → started
    """
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
        if current_status not in ('pending', 'LINK_SENT'):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot start call: status is {current_status}"
            )

        # Update status to started (with timestamp)
        timestamp = datetime.utcnow().isoformat()
        sheets.update_candidate_status(candidate_id, 'started', timestamp)

        # Generate LiveKit token
        scenario = candidate.get('scenario', 'new-lead-discovery-call')
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
        })

        token_obj = (
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

        return {
            "url": LIVEKIT_URL,
            "token": token_obj,
            "room": room,
            "session_id": room,
            "scenario": scenario,
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

        timestamp = datetime.utcnow().isoformat()
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
async def create_token(req: TokenRequest, user: dict = Depends(require_user)) -> dict:
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
async def random_call(user: dict = Depends(require_user)) -> dict:
    scenarios = _all_scenario_summaries()
    if not scenarios:
        raise HTTPException(status_code=404, detail="لا توجد سيناريوهات")
    return {"scenario": random.choice(scenarios)}


@app.post("/api/scenarios/custom")
async def create_custom_scenario(req: CustomScenarioRequest, admin: dict = Depends(require_admin)) -> dict:
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
async def upload_call(file: UploadFile = File(...), admin: dict = Depends(require_admin)) -> dict:
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


@app.get("/api/evaluation")
async def evaluation_categories() -> dict:
    return {"categories": personas.EVALUATION_CATEGORIES}


@app.post("/api/results")
async def save_result(request: Request) -> dict:
    if WEBHOOK_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {WEBHOOK_SECRET}":
            raise HTTPException(status_code=401, detail="Unauthorized")
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
async def get_result(room: str) -> dict:
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
async def diagnostic() -> dict:
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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
