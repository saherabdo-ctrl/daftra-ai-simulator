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

auth.seed()

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


class UserRequest(BaseModel):
    username: str
    name: str = ""
    role: str = "sdr"
    password: str = ""


class BulkUsersRequest(BaseModel):
    base_username: str = "sdr"
    names: list[str]
    role: str = "sdr"


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


def _all_scenario_summaries() -> list[dict]:
    scenarios = personas.scenarios_summary()
    for brief in _custom_briefs.values():
        scenarios.append(personas.custom_scenario_summary(personas.build_custom_scenario(brief)))
    return scenarios


@app.post("/api/login")
async def login(req: LoginRequest) -> dict:
    result = auth.authenticate(req.username.strip(), req.password)
    if not result:
        raise HTTPException(status_code=401, detail="اسم المستخدم أو كلمة المرور غير صحيحة")
    return result


@app.post("/api/logout")
async def logout(authorization: str = Header(None)) -> dict:
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    auth.logout(token)
    return {"ok": True}


@app.get("/api/me")
async def me(user: dict = Depends(require_user)) -> dict:
    return {"user": user}


@app.get("/api/users")
async def list_users(user: dict = Depends(require_admin)) -> dict:
    return {"users": auth.list_users()}


@app.post("/api/users")
async def create_user(req: UserRequest, user: dict = Depends(require_admin)) -> dict:
    if not req.username.strip() or not req.password:
        raise HTTPException(status_code=400, detail="اسم المستخدم وكلمة المرور مطلوبان")
    created = auth.upsert_user(req.username, req.name, req.role, req.password)
    if not created:
        raise HTTPException(status_code=400, detail="اسم مستخدم غير صالح")
    return {"user": created}


@app.post("/api/users/bulk")
async def create_users_bulk(req: BulkUsersRequest, user: dict = Depends(require_admin)) -> dict:
    """ينشئ عدة حسابات دفعة واحدة ويرجع الاعتمادات (كلمة المرور تظهر مرة واحدة فقط)."""
    base = req.base_username.strip() or "sdr"
    if not req.names:
        raise HTTPException(status_code=400, detail="أدخل أسماء المستخدمين (كل اسم في سطر)")
    created = auth.generate_credentials(base, req.names, req.role)
    if not created:
        raise HTTPException(status_code=400, detail="لم تُنشأ أي حسابات")
    return {"users": created}


@app.delete("/api/users/{username}")
async def delete_user(username: str, user: dict = Depends(require_admin)) -> dict:
    if not auth.delete_user(username):
        raise HTTPException(status_code=400, detail="لا يمكن حذف هذا المستخدم")
    return {"ok": True}


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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
