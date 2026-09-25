"""LiveKit voice agent that plays the hidden customer persona during a roleplay.

Run with:  python agent.py dev

The agent connects to the LiveKit project configured in .env as a worker. When
a candidate connects to a room whose token dispatches the agent (AGENT_NAME), the
agent joins, plays the customer persona in real time, and after the call ends
it evaluates the candidate's performance and posts the result to the web server.
"""

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobExecutorType, JobProcess, cli, llm, tts, TurnHandlingOptions
from livekit.plugins import deepgram, groq, openai
from livekit.plugins import silero
import livekit.rtc

from hiringflow_integration import send_result_to_hiringflow

import edge_tts_tts
import daftra_pricing
import elevenlabs_tts
import gemini_tts_tts
import personas

load_dotenv()

logger = logging.getLogger("sdr-agent")
logger.setLevel(logging.INFO)

# Failures worth waking someone up for → chat webhook (alerts.py; off unless
# ALERT_WEBHOOK_URL is set). Attached to named loggers: the LiveKit CLI
# reconfigures the root logger's handlers.
from alerts import AlertLogHandler  # noqa: E402

_alert_handler = AlertLogHandler({
    "entrypoint did not exit in time": "Agent job was force-stopped mid-call",
    "all LLMs are unavailable": "All LLMs failing — the AI client cannot reply",
    "invalid_api_key": "An AI provider rejected its API key",
    "EVALUATION BLOCKED": "Call ended with an empty transcript (no evaluation)",
    "Evaluation failed for room": "Call evaluation crashed",
    "Finalization failed for room": "Call finalization crashed",
    "had no result — marked failed": "Call ended without a result",
    "on_session_end timed out": "Call finalization timed out",
})
for _name in ("sdr-agent", "livekit.agents"):
    logging.getLogger(_name).addHandler(_alert_handler)


# =============================================================================
# Candidate Agent
# =============================================================================

def _validate_env():
    """Validate critical environment variables at startup. Fail fast with clear error."""
    errors = []
    url = os.getenv("LIVEKIT_URL", "").strip()
    key = os.getenv("LIVEKIT_API_KEY", "").strip()
    secret = os.getenv("LIVEKIT_API_SECRET", "").strip()

    if not url:
        errors.append("LIVEKIT_URL is not set")
    elif "your-project" in url or "your_" in url:
        errors.append(f"LIVEKIT_URL contains placeholder value — set real URL from LiveKit Cloud dashboard")

    if not key:
        errors.append("LIVEKIT_API_KEY is not set")
    elif key.startswith("your_") or len(key) < 10:
        errors.append("LIVEKIT_API_KEY appears to be a placeholder")

    if not secret:
        errors.append("LIVEKIT_API_SECRET is not set")
    elif secret.startswith("your_") or len(secret) < 10:
        errors.append("LIVEKIT_API_SECRET appears to be a placeholder")

    if errors:
        print("=" * 60)
        print("FATAL: LiveKit configuration errors — agent cannot start")
        for e in errors:
            print(f"  ✗ {e}")
        print("")
        print("Set these environment variables in your deployment dashboard:")
        print("  LIVEKIT_URL=wss://your-project.livekit.cloud")
        print("  LIVEKIT_API_KEY=your_api_key")
        print("  LIVEKIT_API_SECRET=your_api_secret")
        print("")
        print("Get these from: https://cloud.livekit.io → project → Settings → Keys")
        print("=" * 60)
        raise SystemExit(1)

    logger.info("LiveKit configuration validated ✓")


_validate_env()

AGENT_NAME = os.getenv("AGENT_NAME", "daftra-ai-simulator")
WEBHOOK_URL = os.getenv("EVALUATION_WEBHOOK_URL", "http://localhost:8000/api/results")
WEBHOOK_SECRET = os.getenv("AGENT_WEBHOOK_SECRET", "")
SCENARIOS_BASE_URL = os.getenv("SCENARIOS_BASE_URL", "http://localhost:8000").rstrip("/")
HIRINGFLOW_WEBHOOK_URL = os.getenv("HIRINGFLOW_WEBHOOK_URL", "")
HIRINGFLOW_WEBHOOK_SECRET = os.getenv("HIRINGFLOW_WEBHOOK_SECRET", "")

# رصيد Groq المجاني محدود يوميًا؛ الأفضل إضافة GEMINI_API_KEY لرصيد مجاني أكبر بكثير.
# الاحتياطي الافتراضي openai/gpt-oss-20b (نظيف بلا تسريب تفكير). متاح أيضًا: allam-2-7b، qwen/qwen3.6-27b
GROQ_LLM_MODEL = os.getenv("GROQ_LLM_MODEL", "openai/gpt-oss-20b")
GROQ_EVAL_MODEL = os.getenv("GROQ_EVAL_MODEL", "openai/gpt-oss-20b")
# يحدّ أقصى حجم رد العميل (تقليل استهلاك الرصيد المجاني / TPM). 250 رمزًا ≈ 3-4 جمل.
GROQ_LLM_MAX_TOKENS = int(os.getenv("GROQ_LLM_MAX_TOKENS", "100"))

# gemini-flash-lite-latest ليس نموذج تفكير، لذا 250 رمزًا تكفي (3-4 جمل عامية) وتُسرّع الرد.
GEMINI_LLM_MAX_TOKENS = int(os.getenv("GEMINI_LLM_MAX_TOKENS", "100"))

# Gemini free tier (https://aistudio.google.com/apikey) — مجاني بلا بطاقة. ملاحظة حاسمة:
# gemini-2.5-flash المجاني حده 20 طلبًا في اليوم فقط (429 سريعًا)، لذا الافتراضي هو
# gemini-flash-lite-latest (حدود يومية أعلى بكثير) ويجيد اللهجة السعودية العامية.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_LLM_MODEL = os.getenv("GEMINI_LLM_MODEL", "gemini-flash-lite-latest")
GEMINI_EVAL_MODEL = os.getenv("GEMINI_EVAL_MODEL", "gemini-flash-lite-latest")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# TTS: افتراضيًا Microsoft Edge (مجاني بلا مفتاح، صوت سعودي أصيل للعميل).
# عند تعيين TTS_PROVIDER=elevenlabs + ELEVEN_API_KEY يعمل ElevenLabs بأصوات سعودية
# طبيعية جدًا (معيار تجاري). إن لم يوجد مفتاح يبقى Edge تلقائيًا (fallback).
EDGE_TTS_VOICE = os.getenv("EDGE_TTS_VOICE", "")  # فارغ = تلقائي (سعودي)
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "edge").strip().lower()
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY", "")
GEMINI_TTS_MODEL = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
DEEPGRAM_STT_MODEL = os.getenv("DEEPGRAM_STT_MODEL", "nova-3")
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "ar")
# STT_PROVIDER: "deepgram" (الافتراضي) أو "groq" (Whisper — أدق للهجة المصرية/العامية).
# لكل مكالمة = طلب STT واحد لكل جولة كلام؛ حد Groq المجاني ~20 طلب/دقيقة و2000/يوم (كافٍ للتدريب الفردي).
STT_PROVIDER = os.getenv("STT_PROVIDER", "deepgram").strip().lower()
# whisper-large-v3 (أدق للهجة المصرية لكن أبطأ قليلًا) أو whisper-large-v3-turbo (أسرع وأقل دقة).
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3")
# LLM_PRIMARY: "gemini" (افتراضي — رصيد يومي أكبر ومستقر) أو "groq" (أسرع لكن رصيد Groq اليومي
# محدود ~100k توكن/يوم لكل موديل، أي ~مكالمتان تدريبيتان). الآخر يكون احتياطًا تلقائيًا.
LLM_PRIMARY = os.getenv("LLM_PRIMARY", "gemini").strip().lower()
# مسار حفظ التسجيلات والتقييمات — يُعدّل من .env
RECORDINGS_PATH = os.getenv("RECORDINGS_PATH", os.path.join(os.path.dirname(__file__), "recordings"))


async def _resolve_scenario(ctx: JobContext) -> dict:
    """يختار السيناريو من بيانات الإرسال (أو بادئة اسم الغرفة)، ويجلب ملف العميل المخصص."""
    scenario_id = ctx.room.name.split("_")[0]
    try:
        metadata = json.loads(ctx.job.metadata or "{}")
        scenario_id = metadata.get("scenario") or scenario_id
    except Exception:
        pass

    if scenario_id.startswith("custom_"):
        try:
            headers = {}
            if WEBHOOK_SECRET:
                headers["Authorization"] = f"Bearer {WEBHOOK_SECRET}"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{SCENARIOS_BASE_URL}/api/scenarios/{scenario_id}", headers=headers)
                resp.raise_for_status()
                brief = resp.json().get("brief") or {}
            scenario = personas.build_custom_scenario(brief)
            scenario["id"] = scenario_id
            return scenario
        except Exception:
            logger.exception("Failed to fetch custom scenario %s", scenario_id)

    # Check if AI Client config is provided in metadata (dynamic persona)
    try:
        metadata = json.loads(ctx.job.metadata or "{}")
        ai_client = metadata.get("ai_client") or {}
        if ai_client and ai_client.get("client_id"):
            scenario = _build_scenario_from_ai_client(ai_client, scenario_id)
            if scenario:
                return scenario
    except Exception:
        pass

    if scenario_id in personas.SCENARIOS:
        return personas.SCENARIOS[scenario_id]
    return list(personas.SCENARIOS.values())[0]


DAFTRA_KNOWLEDGE_PATH = os.path.join(os.path.dirname(__file__), "data", "daftra_knowledge.md")


def _load_daftra_knowledge() -> str:
    """يقرأ قاعدة المعرفة العامة عن دفترة (قابلة للتعديل من صفحة الإعدادات)."""
    try:
        with open(DAFTRA_KNOWLEDGE_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""
    except Exception as e:
        logger.warning("تعذّر قراءة قاعدة معرفة دفترة: %s", e)
        return ""


def _build_scenario_from_ai_client(ai_client: dict, scenario_id: str) -> Optional[dict]:
    """Build a scenario dict from an AI Client's dynamic configuration."""
    try:
        import random as _random

        # Start with base scenario if it exists
        if scenario_id in personas.SCENARIOS:
            base = dict(personas.SCENARIOS[scenario_id])
        else:
            base = dict(list(personas.SCENARIOS.values())[0])

        # Override with AI Client config
        name = ai_client.get('name', '')
        customer_name = ai_client.get('customer_name', '') or name
        customer_role = ai_client.get('customer_role', '')
        company_name = ai_client.get('company_name', '')
        business_field = ai_client.get('business_field', '')
        # Country is the single source of truth for dialect (never trust a
        # possibly-stale/independently-set Dialect column) — this guarantees the
        # voice-pool selection below can never mismatch a Saudi client with an
        # Egyptian voice or vice versa.
        country = ai_client.get('country', '') or ('sa' if ai_client.get('dialect') == 'saudi' else 'eg')
        dialect = 'egyptian' if country == 'eg' else 'saudi'
        queue = ai_client.get('queue', '')
        difficulty = ai_client.get('difficulty', 'medium')
        personality = ai_client.get('personality', '')
        instructions = ai_client.get('instructions', '')
        objectives = ai_client.get('objectives', '')
        objections = ai_client.get('objections', '')
        product_brief = ai_client.get('product_brief', '')
        voice = ai_client.get('voice', '')
        temperature = ai_client.get('temperature', 'warm')
        pain_point = ai_client.get('pain_point', '')
        company_size = ai_client.get('company_size', '')
        decision_maker = ai_client.get('decision_maker', True)
        budget_sensitivity = ai_client.get('budget_sensitivity', 'متوسطة')
        buying_intent = ai_client.get('buying_intent', 'متوسطة')
        knowledgeable_raw = ai_client.get('knowledgeable', 'false')

        # Handle random difficulty
        if difficulty == 'random':
            difficulty = _random.choice(['easy', 'medium', 'hard'])
            logger.info("Random difficulty selected: %s", difficulty)

        # Handle random personality
        all_personalities = ['friendly', 'busy', 'hesitant', 'price_sensitive', 'skeptical',
                            'angry', 'confused', 'interested', 'low_intent', 'competitor',
                            'difficult', 'indecisive']
        if personality == 'random':
            personality = _random.choice(all_personalities)
            logger.info("Random personality selected: %s", personality)

        # Handle random knowledgeable (tristate: 'true' / 'false' / 'random')
        if isinstance(knowledgeable_raw, bool):
            knowledgeable = knowledgeable_raw
        elif str(knowledgeable_raw).strip().lower() == 'random':
            knowledgeable = _random.choice([True, False])
            logger.info("Random knowledgeable selected: %s", knowledgeable)
        else:
            knowledgeable = str(knowledgeable_raw).strip().lower() == 'true'

        # Classification-level call duration guidance/limit (min/max minutes) —
        # read live so an admin's change takes effect on the very next call.
        min_duration_minutes = None
        max_duration_minutes = None
        classification_id = ai_client.get('classification_id', '')
        if classification_id:
            try:
                from sheets import get_sheets_client
                classification = get_sheets_client().get_classification(classification_id)
                if classification:
                    raw_min = str(classification.get('min_duration_minutes', '')).strip()
                    raw_max = str(classification.get('max_duration_minutes', '')).strip()
                    if raw_min:
                        min_duration_minutes = float(raw_min)
                    if raw_max:
                        max_duration_minutes = float(raw_max)
            except Exception as e:
                logger.warning("Failed to load classification duration settings: %s", e)

        # صوت عشوائي من نفس اللهجة لما العميل الذكي مالوش صوت محدد صراحة
        if not voice or voice == 'random':
            voice = elevenlabs_tts.random_voice_for_dialect(dialect)
            logger.info("Random voice selected for dialect %s: %s", dialect, voice)

        base['id'] = scenario_id
        base['name'] = f"{customer_name} – {business_field or company_name or scenario_id}"
        base['customer_name'] = customer_name
        base['customer_role'] = customer_role
        base['company_name'] = company_name
        base['business_field'] = business_field
        base['dialect'] = dialect
        base['difficulty'] = difficulty
        base['country'] = country
        base['queue'] = queue
        base['classification_id'] = ai_client.get('classification_id', '')
        base['temperature'] = temperature
        base['voice'] = voice
        base['pain_point'] = pain_point
        base['company_size'] = company_size
        base['decision_maker'] = decision_maker
        base['budget_sensitivity'] = budget_sensitivity
        base['buying_intent'] = buying_intent
        base['knowledgeable'] = knowledgeable
        base['personality'] = personality
        base['min_duration_minutes'] = min_duration_minutes
        base['max_duration_minutes'] = max_duration_minutes

        # Build dynamic persona prompt
        # Customer language: "ar" = Arabic, "en" = English
        customer_language = ai_client.get('customer_language', 'ar')
        is_customer_english = customer_language == "en"

        if is_customer_english:
            dialect_name = "Saudi" if dialect == "saudi" else "Egyptian"
            difficulty_name = {"easy": "easygoing", "medium": "moderate", "hard": "tough"}.get(difficulty, "moderate")
            personality_map = {
                "friendly": "friendly and smiling",
                "busy": "busy and rushed",
                "hesitant": "hesitant and indecisive",
                "price_sensitive": "price-sensitive, asks about cost a lot",
                "skeptical": "skeptical about everything",
                "angry": "annoyed and frustrated",
                "confused": "confused, asks many questions",
                "interested": "enthusiastic and interested",
                "low_intent": "not interested, wants to end call",
                "competitor": "uses a competitor, doesn't want to switch",
                "difficult": "difficult, wants to set own terms",
                "indecisive": "can't decide, changes mind often",
            }
        else:
            dialect_name = "السعودية" if dialect == "saudi" else "المصرية"
            difficulty_name = {"easy": "سهل", "medium": "متوسط", "hard": "صعب"}.get(difficulty, "متوسط")
            personality_map = {
                "friendly": "ودود ومبتسم",
                "busy": "مشغول ومستعجل",
                "hesitant": "متردد ويتراجع كثيرًا",
                "price_sensitive": "حساس للسعر ويسأل عن التكلفة كثيرًا",
                "skeptical": "متشكك في كل شيء",
                "angry": "مزعوج وضيق الصدر",
                "confused": "محتار ويسأل أسئلة كثيرة",
                "interested": "متحمس ومهتم بالمنتج",
                "low_intent": "غير مهتم ويريد إنهاء المكالمة",
                "competitor": "يستخدم منافسًا ولا يريد التغيير",
                "difficult": "صعب ويريد فرض شروطه",
                "indecisive": "لا يعرف يقرر ويتراجع كثيرًا",
            }

        base['dialect_label'] = dialect_name
        base['difficulty_label'] = difficulty_name
        base['personality_label'] = personality_map.get(personality, personality or '')

        personality_desc = ""
        if personality and personality in personality_map:
            if is_customer_english:
                personality_desc = f" Behave like a {personality_map[personality]} person."
            else:
                personality_desc = f" تصرف مثل شخص {personality_map[personality]}."

        if is_customer_english:
            persona_parts = [
                f"You are playing the role of {customer_name}, {customer_role} at {company_name or 'a company'}" + (f" in the {business_field} industry" if business_field else "") + ".",
                f"You MUST speak English only. Do NOT speak Arabic.",
                f"You are an {difficulty_name} customer to deal with." + personality_desc,
                "SPEAK NATURALLY: Complete your sentences fully. Never cut off mid-sentence. "
                "Use natural filler words like 'um', 'well', 'you know' but don't overdo it. "
                "Show real emotions through your tone and word choice, but stay realistic. "
                "React naturally to what the rep says. Keep responses conversational (2-4 sentences).",
            ]
        else:
            # dialect-specific instructions
            dialect_instructions = {
                "saudi": (
                    "Speak in Saudi Arabian dialect (خليجي سعودي عامي). "
                    "Use authentic Saudi expressions like: يالله، ترى، وش، ليش، أيوا، لا، إن شاء الله، ماشاء الله، يعطيك العافية، تسلم، هلا والله، منو، وش رايك. "
                    "Use Saudi sentence patterns. Sound natural like a real Saudi person talking on the phone."
                ),
                "egyptian": (
                    "Speak in Egyptian dialect (مصري عامي). "
                    "Use authentic Egyptian expressions like: إيه، أهلا، عايز، هينفع، ماشي، تمام، إن شاء الله، الله يخليك، تسلم، يا ريت، إزاي، أكيد، بس كده. "
                    "Use Egyptian sentence patterns. Sound natural like a real Egyptian person talking on the phone."
                ),
            }
            dialect_prompt = dialect_instructions.get(dialect, dialect_instructions["saudi"])
            
            persona_parts = [
                f"You are playing the role of {customer_name}, {customer_role} at {company_name or 'a company'}" + (f" in the {business_field} industry" if business_field else "") + ".",
                dialect_prompt,
                f"You are a {difficulty_name} customer to deal with." + personality_desc,
                "SPEAK NATURALLY: Complete your sentences fully. Never cut off mid-sentence. "
                "Use natural filler words like 'يعني', 'أيوه', 'طيب' but don't overdo it. "
                "Show real emotions (surprise, skepticism, interest) through your tone and word choice, "
                "but stay realistic — don't be overly dramatic. "
                "React naturally to what the rep says — laugh when something is funny, "
                "show concern when worried, express doubt when skeptical. "
                "Keep responses conversational length (2-4 sentences typically).",
            ]
            if pain_point:
                persona_parts.append(f"Your main pain point is: {pain_point}.")
            if product_brief:
                persona_parts.append(f"Your business/product (what you sell): {product_brief}.")
            if instructions:
                persona_parts.append(f"Additional instructions: {instructions}.")
            if objectives:
                persona_parts.append(f"Your objectives in this call: {objectives}.")
            if objections:
                persona_parts.append(f"Your potential objections: {objections}.")
            if company_size:
                persona_parts.append(f"Company size: {company_size}.")
            if decision_maker:
                persona_parts.append("You are the decision maker.")
            else:
                persona_parts.append("You are NOT the decision maker — you need to consult your manager.")
            persona_parts.append(f"Budget sensitivity: {budget_sensitivity}. Buying intent: {buying_intent}.")
            if knowledgeable:
                persona_parts.append("You are knowledgeable about accounting/finance.")

        pricing_context = daftra_pricing.format_plans_for_prompt(country)
        if pricing_context:
            persona_parts.append(
                "معلومات مرجعية عن أسعار وباقات دفترة (ممكن يكون العميل شافها على الموقع قبل المكالمة):\n"
                + pricing_context
            )
            persona_parts.append(
                "لو اتكلمتوا عن الأسعار: اسأل أسئلة عامة طبيعية زي عميل حقيقي بيفكر يشتري "
                "(زي: الاشتراك شهري ولا سنوي؟ في خصم لو دفعت سنوي؟ الباقة دي فيها إيه بالظبط؟) "
                "من غير ما تستجوب المندوب أو تسأل عن كل تفصيلة في الباقة. "
                "لو المندوب رشّح لك باقة معينة، ركّز أسئلتك حوالين الباقة دي بس."
            )
        # ملحوظة: قاعدة معرفة دفترة الكاملة (data/daftra_knowledge.md) عمدًا مش
        # بتتحقن هنا — العميل مش مفروض يكون خبير في منتج دفترة (ده شغل المندوب
        # إنه يشرحه)، وحقنها هنا كانت هتتكرر مع كل رد في المكالمة (تكلفة توكنز
        # عالية جدًا). بتتستخدم بدل كده مرة واحدة في تقييم المندوب (_evaluate)
        # ومرة واحدة وقت توليد عملاء جدد (generate_ai_clients).

        # Note: The greeting is sent via session.say() in on_enter.
        # After greeting, respond naturally to the rep's introduction.
        if is_customer_english:
            persona_parts.append(
                "You already greeted the rep. Now wait for them to introduce themselves, "
                "then respond naturally based on your personality. Be conversational and realistic."
            )
        else:
            persona_parts.append(
                "لقد قلت تحية الترحيب بالفعل. الآن انتظر المندوب يقدم نفسه، ثم رد ب طبيعية حسب شخصيتك. "
                "كن واقعيًا في ردودك ولا تكرر التحية."
            )

        # Classification's min-duration is soft guidance for the customer's own
        # conversational pacing (the customer never controls when the call
        # actually ends — that's the rep or the hard max-duration timer below).
        if min_duration_minutes:
            if is_customer_english:
                persona_parts.append(
                    f"This call is expected to last at least {min_duration_minutes:g} minutes — "
                    "don't rush to end the conversation or give one-word answers; stay engaged and let it develop naturally."
                )
            else:
                persona_parts.append(
                    f"المفروض المكالمة دي تستمر {min_duration_minutes:g} دقيقة على الأقل — "
                    "متستعجلش تنهي الكلام أو ترد بكلمة واحدة، خلي نفسك متفاعل واسيب المكالمة تاخد وقتها الطبيعي."
                )

        base['persona_prompt'] = "\n".join(persona_parts)
        base['customer_role'] = customer_role
        base['company_name'] = company_name
        base['business_field'] = business_field
        base['customer_language'] = customer_language

        # Voice selection (use explicit voice if set, otherwise based on dialect and customer language)
        if voice:
            base['voice'] = voice
        elif is_customer_english:
            base['voice'] = "en-US-JennyNeural"
        elif dialect == "egyptian":
            base['voice'] = "ar-EG-SalmaNeural"
        else:
            base['voice'] = "ar-SA-HamedNeural"

        # Set random first_line greeting based on dialect
        if is_customer_english:
            base['first_line'] = _random.choice([
                "Hello?", "Hello, who is this?", "Hi, who am I speaking with?",
                "Good morning.", "Good afternoon."
            ])
        elif dialect == "egyptian":
            base['first_line'] = _random.choice([
                "ألو؟", "ألو مين معاك؟", "السلام عليكم", "هلاو", "نعم؟", "ايوا، معاك مين؟"
            ])
        else:  # saudi
            base['first_line'] = _random.choice([
                "الو؟", "ألو مين معايا؟", "السلام عليكم", "هلا، مين معي؟", "نعم؟", "هلا والله"
            ])

        logger.info("Built dynamic scenario from AI Client %s: %s", ai_client.get('client_id'), base['name'])
        return base
    except Exception as e:
        logger.warning("Failed to build scenario from AI Client: %s", e)
        return None


def _tts_voice_for(scenario: dict) -> str:
    """يختار صوت العميل (ذكر/أنثى حسب السيناريو) أو من متغير البيئة."""
    if EDGE_TTS_VOICE:
        return EDGE_TTS_VOICE
    return scenario.get("voice") or edge_tts_tts.VOICES.get(
        scenario.get("dialect", ""), edge_tts_tts.DEFAULT_VOICE
    )


def _make_tts(scenario: dict):
    """يوّلِف مكوّن الصوت: Gemini TTS (طبيعي جدًا بنفس مفتاح Gemini) إن اختير،
    وإلا ElevenLabs، وإلا Edge (fallback مجاني)."""
    if TTS_PROVIDER == "gemini" and GEMINI_API_KEY:
        logger.info("TTS: Gemini TTS (%s) — أصوات عربية طبيعية بمشاعر", GEMINI_TTS_MODEL)
        return gemini_tts_tts.GeminiTTS(api_key=GEMINI_API_KEY, model=GEMINI_TTS_MODEL)
    if TTS_PROVIDER == "elevenlabs" and ELEVEN_API_KEY:
        logger.info("TTS: ElevenLabs (أصوات سعودية) + Edge كاحتياط عند فشل/تعليق مؤقت")
        return tts.FallbackAdapter([
            elevenlabs_tts.ElevenLabsTTS(voice=scenario.get("voice")),
            edge_tts_tts.EdgeTTS(voice=_tts_voice_for(scenario)),
        ])
    logger.info("TTS: Microsoft Edge (مجاني)")
    return edge_tts_tts.EdgeTTS(voice=_tts_voice_for(scenario))


def _make_llm(*, max_completion_tokens: int | None = None):
    """يبني نموذج اللغة مع احتياط تلقائي بين Groq (سريع) وGemini (رصيد أكبر)،
    ويتحكم في الأساسي LLM_PRIMARY. عند فشل الأساسي (429/خطأ/تعليق) ننتقل فورًا للآخر.
    timeout صريح يقطع أي اتصال «معلّق» (كان يسبب صمتًا لعدة دقائق) فيرتد الاحتياط بسرعة."""
    timeout = httpx.Timeout(connect=15.0, read=15.0, write=10.0, pool=10.0)

    def _gemini():
        return openai.LLM(
            model=GEMINI_LLM_MODEL,
            base_url=GEMINI_BASE_URL,
            api_key=GEMINI_API_KEY,
            timeout=timeout,
            # Gemini 2.5+ نماذج «تفكير»: نستخدم ميزانيته الخاصة لضمان رد كامل بلا اقتطاع.
            max_completion_tokens=GEMINI_LLM_MAX_TOKENS,
        )

    def _groq():
        return groq.LLM(
            model=GROQ_LLM_MODEL,
            max_completion_tokens=max_completion_tokens,
            timeout=timeout,
        )

    llms: list[llm.LLM] = []
    if LLM_PRIMARY == "groq" and os.getenv("GROQ_API_KEY"):
        logger.info("LLM[0]: Groq (%s) — أساسي (سريع)", GROQ_LLM_MODEL)
        llms.append(_groq())
        if GEMINI_API_KEY:
            logger.info("LLM[%d]: Gemini (%s) — احتياط", len(llms), GEMINI_LLM_MODEL)
            llms.append(_gemini())
    else:
        if GEMINI_API_KEY:
            logger.info("LLM[0]: Gemini (%s) — أساسي (رصيد أكبر)", GEMINI_LLM_MODEL)
            llms.append(_gemini())
        if os.getenv("GROQ_API_KEY"):
            logger.info("LLM[%d]: Groq (%s) — احتياط", len(llms), GROQ_LLM_MODEL)
            llms.append(_groq())
    if len(llms) == 1:
        return llms[0]
    if not llms:
        raise RuntimeError("No LLM configured: set GEMINI_API_KEY or GROQ_API_KEY in .env")
    return llm.FallbackAdapter(llms, attempt_timeout=6.0)


def _make_stt():
    """بُنية التعرف على الكلام: Groq Whisper للعامية المصرية، وإلا Deepgram (افتراضي)."""
    if STT_PROVIDER == "groq" and os.getenv("GROQ_API_KEY"):
        logger.info("STT: Groq Whisper (%s) — لغة %s", GROQ_STT_MODEL, STT_LANGUAGE)
        return groq.STT(model=GROQ_STT_MODEL, language=STT_LANGUAGE)
    logger.info("STT: Deepgram (%s) — لغة %s", DEEPGRAM_STT_MODEL, STT_LANGUAGE)
    return deepgram.STT(model=DEEPGRAM_STT_MODEL, language=STT_LANGUAGE)


def _make_eval_client():
    """عميل التقييم الأساسي: Gemini (يُستخدم فقط إن وُجد مفتاحه؛ الاحتياط في _evaluate)."""
    return AsyncOpenAI(api_key=GEMINI_API_KEY, base_url=GEMINI_BASE_URL), GEMINI_EVAL_MODEL


import wave as _wave
import os as _os

_RINGTONE_PATH = _os.path.join(_os.path.dirname(__file__), "static", "ring.wav")


async def _ringtone_frames(chunk_ms: int = 20):
    """يقرأ ملف الرنين (WAV PCM 16-bit) ويُرجع إطارات صوتية لـLiveKit."""
    if not _os.path.exists(_RINGTONE_PATH):
        return
    with _wave.open(_RINGTONE_PATH, "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        frames_per_chunk = int(sr * chunk_ms / 1000)
        bytes_per_chunk = frames_per_chunk * ch * sw
        while True:
            raw = wf.readframes(frames_per_chunk)
            if not raw:
                break
            yield livekit.rtc.AudioFrame(
                data=raw,
                sample_rate=sr,
                num_channels=ch,
                samples_per_channel=len(raw) // (ch * sw),
            )


class CustomerAgent(Agent):
    def __init__(self, scenario: dict, room_name: str, room=None, username: str = "", industry: str = "", test_session_id: str = "", candidate_id: str = "", candidate_email: str = "", candidate_name: str = "", user_type: str = "INTERNAL", callback_url: str = "", call_type: str = "AUDIO_CALL", attempt_id: str = "", call_id: str = "") -> None:
        self.scenario = scenario
        self.room_name = room_name
        self.room = room
        self.username = username
        self.industry = industry
        self.test_session_id = test_session_id
        self.candidate_id = candidate_id
        self.candidate_email = candidate_email
        self.candidate_name = candidate_name or username
        self.user_type = user_type
        self.callback_url = callback_url
        self.call_type = call_type
        self.attempt_id = attempt_id
        self.call_id = call_id
        self.started_at = time.monotonic()
        self.transcript: list[dict] = []
        self._seen_ids: set = set()
        self._reported = False
        self._call_dir: str = ""
        self._max_duration_task = None
        super().__init__(instructions=scenario["persona_prompt"])

    def _get_call_dir(self) -> str:
        """يُرجع مسار مجلد المكالمة: {username} - {Industry}/{مُعرّف فريد للمكالمة}.
        المُعرّف: AttemptID (مرشحين) أو CallID (عملاء الأدمن الذكيين) أو اسم الـ room
        كحل أخير — لازم يكون فريد دايمًا، وإلا مكالمتين لنفس الاسم/الصناعة هيتكتب
        تسجيلهم وPDF بعض فوق بعض في نفس المجلد."""
        if self._call_dir:
            return self._call_dir
        name = self.username or "unknown"
        ind = self.industry or "general"
        folder_name = f"{name} - {ind}"
        unique_id = self.attempt_id or self.call_id or self.room_name
        # إزالة الأحرف غير المسموح بها في أسماء المجلدات
        for ch in r'<>:"/\|?*':
            folder_name = folder_name.replace(ch, "_")
            unique_id = unique_id.replace(ch, "_")
        self._call_dir = os.path.join(RECORDINGS_PATH, folder_name, unique_id)
        return self._call_dir

    async def _send_ring_data(self) -> None:
        """يرسل رسالة data للـfrontend لتشغيل رنين التليفون.

        عند دخول on_enter، اتصال الغرفة أحيانًا لسه مش مكتمل تمامًا
        (local_participant لسه مش جاهز)، فبنعيد المحاولة بفاصل قصير
        بدل ما نستسلم من أول فشل.
        """
        if not self.room:
            return
        import json as _json
        payload = _json.dumps({"type": "ring"}).encode("utf-8")
        max_attempts = 15
        for attempt in range(max_attempts):
            try:
                await self.room.local_participant.publish_data(payload, topic="ring")
                logger.info("Ring data sent to frontend (attempt %d)", attempt + 1)
                return
            except Exception as e:
                if attempt == max_attempts - 1:
                    logger.warning("Ring data failed after retries: %s", e)
                else:
                    await asyncio.sleep(0.3)

    async def _auto_end_after_max_duration(self, max_duration_minutes: float) -> None:
        """Classification-level hard call-duration limit — ends the call from
        the agent's side once reached, if the human hasn't already ended it.

        Closes the SESSION, not the room: on_exit() → finalization runs as for a
        normal hang-up. Disconnecting the room here made LiveKit shut the job
        down, which cancelled finalization mid-way (no evaluation, call stuck
        "in progress") and left the human in a silent room."""
        try:
            await asyncio.sleep(max_duration_minutes * 60)
            logger.info(
                "Max call duration (%.1f min) reached for room %s — ending call",
                max_duration_minutes, self.room_name,
            )
            # يبلغ الـfrontend إن المكالمة خلصت عشان يقفل ويعرض التقييم
            if self.room:
                try:
                    payload = json.dumps({"type": "call_ended", "reason": "max_duration"}).encode("utf-8")
                    await self.room.local_participant.publish_data(payload, topic="call_ended")
                except Exception as e:
                    logger.warning("call_ended data failed for room %s: %s", self.room_name, e)
            self.session.shutdown(drain=True)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("Auto-end-call timer failed for room %s: %s", self.room_name, e)

    async def on_enter(self) -> None:
        logger.info("Call started for room %s", self.room_name)
        max_duration_minutes = self.scenario.get('max_duration_minutes')
        if max_duration_minutes:
            self._max_duration_task = asyncio.create_task(self._auto_end_after_max_duration(max_duration_minutes))
        # ارسال رنين التليفون للـfrontend فورًا (رنة واحدة 4 ثواني)
        await self._send_ring_data()
        # انتظار حتى يخلص الرنين
        await asyncio.sleep(1.5)
        # العميل يرد بأول تحية
        try:
            await self.session.say(self.scenario["first_line"])
        except Exception as e:
            logger.warning("Failed to send first_line: %s", e)

    async def on_exit(self) -> None:
        logger.info("Call ended for room %s (%d turns)", self.room_name, len(self.transcript))
        if self._max_duration_task and not self._max_duration_task.done():
            self._max_duration_task.cancel()
        self._save_call_data()
        if hasattr(self, '_call_ended_event') and self._call_ended_event is not None:
            self._call_ended_event.set()

    def _save_call_data(self) -> None:
        """يحفظ النص في مجلد المكالمة."""
        try:
            call_dir = self._get_call_dir()
            os.makedirs(call_dir, exist_ok=True)
            with open(os.path.join(call_dir, "transcript.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "room": self.room_name,
                    "scenario": self.scenario.get("id", ""),
                    "username": self.username,
                    "industry": self.industry,
                    "duration_seconds": round(time.monotonic() - self.started_at),
                    "transcript": self.transcript,
                }, f, ensure_ascii=False, indent=2)
            logger.info("Call data saved to %s", call_dir)
        except Exception as e:
            logger.warning("Failed to save call data: %s", e)

    async def evaluate_and_report(self, recording_data: dict = None) -> None:
        """Evaluate the SDR once the call is over and post the result. Safe to call twice."""
        if self._reported:
            logger.info("Evaluation already reported for room %s — skipping duplicate", self.room_name)
            return
        try:
            duration = max(0, round(time.monotonic() - self.started_at))
            result = None

            # CRITICAL: Only evaluate if we have actual transcript turns
            if not self.transcript:
                logger.error("EVALUATION BLOCKED: No transcript turns recorded — refusing to generate fallback score=0")
                logger.error("This indicates STT/recording pipeline failure. Candidate result NOT written to Sheets.")
                return

            try:
                result = await self._evaluate(duration)
            except Exception as e:
                logger.warning("LLM evaluation failed for room %s: %s", self.room_name, e)

            if result:
                logger.info(
                    "Evaluation ready for room %s: score=%.1f grade=%s — posting result",
                    self.room_name, result.get("score", 0.0), result.get("grade", "?"),
                )
                # Save evaluation to call directory
                try:
                    call_dir = self._get_call_dir()
                    os.makedirs(call_dir, exist_ok=True)
                    eval_path = os.path.join(call_dir, "evaluation.json")
                    with open(eval_path, "w", encoding="utf-8") as f:
                        json.dump(result, f, ensure_ascii=False, indent=2)
                    # Generate PDF evaluation report
                    pdf_path = self._generate_eval_pdf(result)
                    if pdf_path:
                        result["_pdf_path"] = pdf_path
                except Exception as e:
                    logger.warning("Failed to save evaluation: %s", e)
            else:
                # LLM eval failed but we HAVE transcript - build minimal result from transcript
                logger.warning("Using minimal result for room %s (LLM eval failed but transcript exists)", self.room_name)
                result = {
                    "scenario": self.scenario.get("id", ""),
                    "scenario_name": self.scenario.get("name", ""),
                    "customer_name": self.scenario.get("customer_name", ""),
                    "classification_id": self.scenario.get("classification_id", ""),
                    "duration_seconds": duration,
                    "transcript": self.transcript,
                    "criteria": [],
                    "overall_score": 0,
                    "strengths": [],
                    "areas_for_improvement": [],
                    "coaching": "LLM evaluation unavailable; transcript recorded but not scored.",
                    "next_step_action": "",
                    "lead_status": "unrated",
                    "lead_status_reason": "LLM evaluation failed — transcript available",
                }
                try:
                    call_dir = self._get_call_dir()
                    os.makedirs(call_dir, exist_ok=True)
                    eval_path = os.path.join(call_dir, "evaluation.json")
                    with open(eval_path, "w", encoding="utf-8") as f:
                        json.dump(result, f, ensure_ascii=False, indent=2)
                    # Generate PDF evaluation report
                    pdf_path = self._generate_eval_pdf(result)
                    if pdf_path:
                        result["_pdf_path"] = pdf_path
                except Exception as e:
                    logger.warning("Failed to save minimal evaluation: %s", e)

            # Post result to local webhook
            await self._post_result(result)

            # Update call in Calls sheet (for ALL users, not just candidates)
            try:
                from sheets import get_sheets_client
                sheets = get_sheets_client()
                overall_score = result.get('overall_score', 0) if result else 0
                sheets.update_call_end(self.room_name, overall_score)
                logger.info("Call end updated in Calls sheet: room=%s, score=%s", self.room_name, overall_score)
            except Exception as e:
                logger.warning("Failed to update call end in Calls sheet: %s", e)

            # Handle candidate-specific post-call actions
            if self.user_type == "CANDIDATE":
                await self._handle_candidate_completion(result, recording_data or {})

            # Send results to HiringFlow if callback_url is configured
            if self.callback_url:
                await self._send_hiringflow_callback(result, recording_data or {})

            self._reported = True
        except Exception:
            logger.exception("Evaluation failed for room %s", self.room_name)

    async def _handle_candidate_completion(self, result: dict, recording_data: dict = None) -> None:
        """Handle candidate-specific completion: update status in Google Sheets."""
        if not self.candidate_id:
            return
        if not recording_data:
            recording_data = {}

        # CRITICAL: Only write result to Sheets if evaluation produced a valid score
        # Do NOT write score=0/Failed when evaluation infrastructure failed
        if not result or result.get('overall_score', 0) == 0 and result.get('lead_status') == 'unrated':
            logger.warning("Candidate %s: skipping Sheets result update — evaluation incomplete/invalid", self.candidate_id)
            # Still mark call as ended but don't set pass/fail
            try:
                from sheets import get_sheets_client
                sheets = get_sheets_client()
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                sheets.update_candidate_status(self.candidate_id, 'ended', timestamp)
                logger.info("Candidate %s status set to 'ended' (no score)", self.candidate_id)
            except Exception as e:
                logger.warning("Failed to update candidate status: %s", e)
            return

        # Step 1: Update sheet (score, result, status) — must succeed
        try:
            from sheets import get_sheets_client
            sheets = get_sheets_client()

            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            sheets.update_candidate_status(self.candidate_id, 'ended', timestamp)

            overall_score = result.get('overall_score', 0) if result else 0
            pass_result = 'Passed' if overall_score >= 70 else 'Failed'
            sheets.update_candidate_result(self.candidate_id, overall_score, pass_result)

            logger.info("Candidate %s sheet updated: score=%d, result=%s",
                       self.candidate_id, overall_score, pass_result)
        except Exception as e:
            logger.warning("Failed to update candidate sheet: %s", e)
            return

        # Step 2: Update recording URLs and evaluation link from Drive
        try:
            from sheets import get_sheets_client
            from drive_upload import get_drive_client, find_or_create_folder, get_shareable_link, FOLDER_NAME
            sheets = get_sheets_client()

            # Update recording URLs in Sheets
            audio_url = recording_data.get('audio_recording', {}).get('url', '')
            video_url = recording_data.get('video_recording', {}).get('url', '')
            drive_folder_id = recording_data.get('drive_folder_id', '')
            if audio_url or video_url or drive_folder_id:
                sheets.update_candidate_recordings(self.candidate_id, drive_folder_id, audio_url, video_url)

            # Get evaluation link from Shared Drive
            drive = get_drive_client()
            candidate_folder_name = f"{self.candidate_name or self.username or 'unknown'} - {self.candidate_id}" if self.candidate_id else (self.username or "unknown")
            eval_link = get_shareable_link(drive, self.candidate_id, "evaluation.html", parent_name=FOLDER_NAME)
            if eval_link:
                sheets.update_candidate_evaluation(self.candidate_id, eval_link)
                logger.info("Candidate %s evaluation link written: %s", self.candidate_id, eval_link)

            # Upload PDF evaluation to Drive
            call_dir = self._get_call_dir()
            pdf_path = os.path.join(call_dir, "evaluation.pdf")
            if os.path.exists(pdf_path):
                try:
                    from drive_upload import upload_file, _find_folder, FOLDER_NAME
                    # Find the candidate's folder
                    parent_id = _find_folder(drive, FOLDER_NAME) if FOLDER_NAME else None
                    folder_id = _find_folder(drive, self.candidate_id, parent_id)
                    if folder_id:
                        upload_file(drive, pdf_path, folder_id, "evaluation.pdf")
                        logger.info("Candidate %s PDF evaluation uploaded to Drive", self.candidate_id)
                except Exception as e:
                    logger.warning("PDF upload failed: %s", e)
        except Exception as e:
            logger.warning("Evaluation link update failed for %s: %s", self.candidate_id, e)

    async def _upload_to_drive(self, result: dict) -> Optional[str]:
        """DEPRECATED: Drive upload is now handled by the entrypoint function using LiveKit Egress."""
        return None

    def _generate_eval_card(self, result: dict) -> str:
        """Generate an HTML evaluation card."""
        if not result:
            return ""

        candidate_name = self.candidate_name or self.username or "Candidate"
        score = result.get("overall_score", 0)
        pass_fail = "Passed" if score >= 70 else "Failed"
        criteria = result.get("criteria", [])
        config_error = result.get("config_error", "")
        strengths = result.get("strengths", [])
        improvements = result.get("areas_for_improvement", [])
        coaching = result.get("coaching", "")
        lead_status = result.get("lead_status", "")
        transcript = result.get("transcript", [])

        transcript_html = ""
        for m in transcript:
            role = "SDR" if m.get("role") == "user" else "Customer"
            text = m.get("text", "")
            transcript_html += f'<p><strong>{role}:</strong> {text}</p>\n'

        if config_error:
            scores_html = f'<p style="color:#e74c3c;font-weight:bold;">{config_error}</p>\n'
        elif not criteria:
            scores_html = "<p>No criteria evaluated</p>\n"
        else:
            scores_html = ""
            for c in criteria:
                label = c.get("criterion", "")
                val = c.get("score", 0)
                weight = c.get("weight", 0)
                color = "#27ae60" if val >= 61 else ("#f39c12" if val >= 31 else "#e74c3c")
                scores_html += (
                    f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #eee;">'
                    f'<span>{label} ({weight:g}%)</span><span style="color:{color};font-weight:bold;">{val:g}/100</span></div>\n'
                )

        strengths_html = "".join(f"<li>{s}</li>" for s in strengths) if strengths else "<li>None identified</li>"
        improvements_html = "".join(f"<li>{s}</li>" for s in improvements) if improvements else "<li>None identified</li>"

        return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Evaluation - {candidate_name}</title>
<style>
  body {{ font-family: Arial, sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; color: #333; }}
  .header {{ background: #2c3e50; color: white; padding: 1.5rem; border-radius: 8px 8px 0 0; }}
  .header h1 {{ margin: 0 0 0.5rem 0; font-size: 1.5rem; }}
  .header .meta {{ opacity: 0.9; font-size: 0.9rem; }}
  .section {{ padding: 1.2rem; border: 1px solid #e0e0e0; border-top: none; }}
  .section:last-child {{ border-radius: 0 0 8px 8px; }}
  .score-big {{ font-size: 3rem; font-weight: bold; text-align: center; margin: 1rem 0; }}
  .passed {{ color: #27ae60; }}
  .failed {{ color: #e74c3c; }}
  .category-bar {{ display: flex; align-items: center; gap: 0.5rem; }}
  .bar {{ flex: 1; height: 8px; background: #eee; border-radius: 4px; }}
  .bar-fill {{ height: 100%; border-radius: 4px; }}
  .transcript {{ background: #f8f9fa; padding: 1rem; border-radius: 6px; max-height: 400px; overflow-y: auto; font-size: 0.9rem; }}
  .transcript p {{ margin: 0.3rem 0; }}
</style></head>
<body>
  <div class="header">
    <h1>Evaluation Report</h1>
    <div class="meta"><strong>{candidate_name}</strong> | Scenario: {self.scenario.get("name", "")} | Date: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}</div>
  </div>

  <div class="section" style="text-align:center;">
    <div class="score-big {"passed" if score >= 70 else "failed"}">{score}/100</div>
    <div style="font-size:1.3rem;font-weight:bold;color:{"#27ae60" if score >= 70 else "#e74c3c"};">{pass_fail}</div>
  </div>

  <div class="section">
    <h3>Scores by Criterion</h3>
    {scores_html}
  </div>

  <div class="section">
    <h3>Strengths</h3>
    <ul>{strengths_html}</ul>
  </div>

  <div class="section">
    <h3>Areas for Improvement</h3>
    <ul>{improvements_html}</ul>
  </div>

  <div class="section">
    <h3>Coaching Notes</h3>
    <p>{coaching}</p>
  </div>

  <div class="section">
    <h3>Lead Status: <span style="text-transform:uppercase;">{lead_status}</span></h3>
  </div>

  <div class="section">
    <h3>Transcript</h3>
    <div class="transcript">{transcript_html if transcript_html else "<p>No transcript available.</p>"}</div>
  </div>
</body>
</html>"""

    def _generate_eval_pdf(self, result: dict) -> Optional[str]:
        """Generate a PDF evaluation report (بدعم كامل للعربي). Returns the file path or None."""
        if not result:
            return None
        try:
            from fpdf import FPDF
            from fpdf.enums import XPos, YPos

            def txt(value) -> str:
                return "" if value is None else str(value)

            candidate_name = txt(self.candidate_name or self.username or "Candidate")
            score = result.get("overall_score", 0)
            pass_fail = "Passed" if score >= 70 else "Failed"
            criteria = result.get("criteria", [])
            config_error = result.get("config_error", "")
            strengths = result.get("strengths", [])
            improvements = result.get("areas_for_improvement", [])
            coaching = result.get("coaching", "")
            lead_status = result.get("lead_status", "")
            transcript = result.get("transcript", [])
            scn = self.scenario or {}

            pdf = FPDF()
            pdf.set_auto_page_break(auto=True, margin=15)
            # خط Amiri (يدعم العربي والإنجليزي معًا) + محرك تشكيل HarfBuzz لربط
            # الحروف العربية وترتيب النص RTL بشكل صحيح تلقائيًا.
            font_dir = os.path.join(os.path.dirname(__file__), "fonts")
            pdf.add_font("Amiri", "", os.path.join(font_dir, "Amiri-Regular.ttf"))
            pdf.add_font("Amiri", "B", os.path.join(font_dir, "Amiri-Bold.ttf"))
            pdf.set_text_shaping(True)
            pdf.add_page()

            font = "Amiri"
            font_b = "Amiri"

            # Header
            pdf.set_fill_color(44, 62, 80)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font(font_b, "B", 18)
            pdf.cell(0, 15, "Evaluation Report", new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=True, align="C")
            pdf.set_font(font, "", 10)
            scenario_name = txt(scn.get('name', ''))
            date_str = datetime.now().strftime('%Y-%m-%d %H:%M')
            pdf.cell(0, 8, f"{candidate_name} | {scenario_name} | {date_str}", new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=True, align="C")
            pdf.ln(5)

            # Score
            pdf.set_text_color(0, 0, 0)
            if score >= 70:
                pdf.set_text_color(39, 174, 96)
            else:
                pdf.set_text_color(231, 76, 60)
            pdf.set_font(font_b, "B", 36)
            pdf.cell(0, 20, f"{score}/100", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
            pdf.set_font(font_b, "B", 14)
            pdf.cell(0, 10, pass_fail, new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
            pdf.ln(3)

            # Customer Persona — شخصية العميل اللي عمل المكالمة مع الموظف
            persona_rows = []

            def add_persona(label: str, value) -> None:
                if value not in (None, ""):
                    persona_rows.append((label, value))

            add_persona("Customer", scn.get('customer_name'))
            add_persona("Role", scn.get('customer_role'))
            add_persona("Company", scn.get('company_name'))
            add_persona("Industry", scn.get('business_field'))
            add_persona("Dialect", scn.get('dialect_label') or scn.get('dialect'))
            add_persona("Difficulty", scn.get('difficulty_label') or scn.get('difficulty'))
            add_persona("Personality", scn.get('personality_label') or scn.get('personality'))
            if scn.get('temperature'):
                add_persona("Knows Daftra?", "Yes (warm)" if scn.get('temperature') == 'warm' else "No (cold)")
            if 'decision_maker' in scn:
                add_persona("Decision Maker", "Yes" if scn.get('decision_maker') else "No")
            add_persona("Budget Sensitivity", scn.get('budget_sensitivity'))
            add_persona("Buying Intent", scn.get('buying_intent'))
            add_persona("Company Size", scn.get('company_size'))
            add_persona("Pain Point", scn.get('pain_point'))

            if persona_rows:
                pdf.set_text_color(0, 0, 0)
                pdf.set_font(font_b, "B", 13)
                pdf.cell(0, 10, "Customer Persona", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_draw_color(200, 200, 200)
                for label, value in persona_rows:
                    pdf.set_font(font, "", 11)
                    pdf.cell(50, 8, label, border="B")
                    pdf.set_font(font_b, "B", 11)
                    pdf.multi_cell(0, 8, txt(value), border="B", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.ln(3)

            # Scores by Criterion (dynamic, per-Classification checklist from Google Sheets)
            pdf.set_text_color(0, 0, 0)
            pdf.set_font(font_b, "B", 13)
            pdf.cell(0, 10, "Scores by Criterion", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_draw_color(200, 200, 200)
            if config_error:
                pdf.set_text_color(231, 76, 60)
                pdf.set_font(font, "", 10)
                pdf.multi_cell(0, 7, txt(config_error), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            elif not criteria:
                pdf.set_font(font, "", 10)
                pdf.cell(0, 8, "No criteria evaluated", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            for c in criteria:
                val = c.get("score", 0)
                weight = c.get("weight", 0)
                label = txt(f'{c.get("criterion", "")} ({weight:g}%)' if isinstance(weight, (int, float)) else c.get("criterion", ""))
                if val >= 61:
                    pdf.set_text_color(39, 174, 96)
                elif val >= 31:
                    pdf.set_text_color(243, 156, 18)
                else:
                    pdf.set_text_color(231, 76, 60)
                pdf.set_font(font, "", 11)
                pdf.cell(120, 8, label, border="B")
                pdf.set_font(font_b, "B", 11)
                pdf.cell(0, 8, f"{val:g}/100", new_x=XPos.LMARGIN, new_y=YPos.NEXT, border="B", align="R")
            pdf.ln(3)

            # Strengths
            pdf.set_text_color(0, 0, 0)
            pdf.set_font(font_b, "B", 13)
            pdf.cell(0, 10, "Strengths", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font(font, "", 10)
            for s in strengths:
                pdf.multi_cell(0, 7, f"- {txt(s)}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            if not strengths:
                pdf.cell(0, 7, "None identified", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)

            # Areas for Improvement
            pdf.set_font(font_b, "B", 13)
            pdf.cell(0, 10, "Areas for Improvement", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font(font, "", 10)
            for s in improvements:
                pdf.multi_cell(0, 7, f"- {txt(s)}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            if not improvements:
                pdf.cell(0, 7, "None identified", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)

            # Coaching Notes
            pdf.set_font(font_b, "B", 13)
            pdf.cell(0, 10, "Coaching Notes", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font(font, "", 10)
            pdf.multi_cell(0, 7, txt(coaching or "No coaching notes."), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)

            # Lead Status
            pdf.set_font(font_b, "B", 13)
            pdf.cell(0, 10, f"Lead Status: {txt(lead_status.upper() if lead_status else 'N/A')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)

            # Transcript
            pdf.set_font(font_b, "B", 13)
            pdf.cell(0, 10, "Transcript", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font(font, "", 9)
            pdf.set_fill_color(248, 249, 250)
            for m in transcript:
                role = "SDR" if m.get("role") == "user" else "Customer"
                text = txt(m.get("text", ""))
                line = f"{role}: {text}"
                pdf.multi_cell(0, 6, line, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            if not transcript:
                pdf.cell(0, 7, "No transcript available.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            # Save PDF
            call_dir = self._get_call_dir()
            os.makedirs(call_dir, exist_ok=True)
            pdf_path = os.path.join(call_dir, "evaluation.pdf")
            pdf.output(pdf_path)
            logger.info("Evaluation PDF saved: %s (%d bytes)", pdf_path, os.path.getsize(pdf_path))
            return pdf_path

        except Exception as e:
            logger.warning("Failed to generate evaluation PDF: %s", e, exc_info=True)
            return None

    async def _send_hiringflow_callback(self, result: dict, recording_data: dict = None) -> None:
        """Send evaluation result to HiringFlow webhook with recording URLs."""
        if not recording_data:
            recording_data = {}
        drive_folder_id = recording_data.get("drive_folder_id", "")
        audio_recording = recording_data.get("audio_recording")
        video_recording = recording_data.get("video_recording")
        try:
            from hiringflow_integration import send_result_to_hiringflow
            response = await send_result_to_hiringflow(
                test_session_id=self.test_session_id,
                candidate_id=self.candidate_id,
                score=result.get("overall_score", 0),
                evaluation=result,
                transcript=self.transcript,
                drive_folder_id=drive_folder_id,
                audio_recording=audio_recording,
                video_recording=video_recording,
            )
            if response.get("success"):
                logger.info("HiringFlow callback sent successfully (attempts=%d)", response.get("attempts"))
            else:
                logger.warning("HiringFlow callback failed: %s", response.get("error"))
        except Exception as e:
            logger.warning("HiringFlow callback failed: %s", e)

    def record_item(self, item: Any) -> None:
        try:
            # `conversation_item_added` passes a ConversationItemAddedEvent whose
            # `.item` is the ChatMessage (text lives in `.text_content`).
            if hasattr(item, "item"):
                item = item.item
            if isinstance(item, dict):
                role, text = item.get("role"), item.get("text")
                key = item.get("id") or (role, text)
            else:
                role = getattr(item, "role", None)
                text = getattr(item, "text_content", None) or getattr(item, "text", None)
                key = getattr(item, "id", None) or (role, text)
            if role in ("user", "assistant") and text:
                if key not in self._seen_ids:
                    self._seen_ids.add(key)
                    self.transcript.append({"role": role, "text": str(text).strip()})
        except Exception:
            pass

    async def _evaluate(self, duration: int) -> dict | None:
        if not self.transcript:
            logger.warning("No transcript to evaluate for room %s", self.room_name)
            return None

        lines = [
            ("SDR" if m["role"] == "user" else "CUSTOMER") + ": " + m["text"]
            for m in self.transcript
        ]

        from sheets import get_sheets_client
        sheets_client = get_sheets_client()

        classification_id = self.scenario.get("classification_id", "")
        classification = sheets_client.get_classification(classification_id) if classification_id else None
        classification_name = (classification or {}).get("name", "").strip()

        base_result = {
            "scenario": self.scenario["id"],
            "scenario_name": self.scenario["name"],
            "customer_name": self.scenario["customer_name"],
            "classification_id": classification_id,
            "classification_name": classification_name,
            "duration_seconds": duration,
            "transcript": self.transcript,
            "criteria": [],
            "overall_score": 0,
            "strengths": [],
            "areas_for_improvement": [],
            "coaching": "",
            "next_step_action": "",
            "lead_status": "",
            "lead_status_reason": "",
        }

        if not classification_name:
            msg = (
                f"لا يوجد تصنيف (Classification) مرتبط بهذا العميل الذكي (classification_id={classification_id!r})، "
                "فلا يمكن تحديد قائمة التقييم (Test Calls Checklist). تم إيقاف التقييم."
            )
            logger.error("Evaluation blocked for room %s: %s", self.room_name, msg)
            return {**base_result, "config_error": msg}

        raw_checklist = sheets_client.get_checklist(classification_name)
        if raw_checklist is None:
            msg = f'تعذّر قراءة قائمة التقييم (Test Calls Checklist) للتصنيف "{classification_name}" من Google Sheets.'
            logger.error("Evaluation blocked for room %s: %s", self.room_name, msg)
            return {**base_result, "config_error": msg}

        try:
            checklist = personas.validate_checklist(classification_name, raw_checklist)
        except personas.ChecklistError as e:
            logger.error("Evaluation blocked for room %s: %s", self.room_name, e)
            return {**base_result, "config_error": str(e)}

        checklist_version = str(checklist[0].get("version", "") or "") if checklist else ""
        eval_system_prompt = personas.build_evaluation_prompt(classification_name, checklist)
        reference_parts = []
        pricing_ref = daftra_pricing.format_plans_for_prompt(self.scenario.get('country', ''))
        if pricing_ref:
            reference_parts.append(pricing_ref)
        knowledge_ref = _load_daftra_knowledge()
        if knowledge_ref:
            reference_parts.append(knowledge_ref)
        if reference_parts:
            eval_system_prompt += (
                "\n\nمعلومات مرجعية عن دفترة (أسعار حالية ومعلومات عامة) — استخدمها في التقييم: "
                "لو المندوب ذكر سعرًا أو تفاصيل باقة مخالفة للمرجع ده، اعتبرها نقطة سلبية واذكرها صراحة "
                'في "areas_for_improvement". لو سياق المكالمة استلزم توضيح الفرق بين "المستخدم" و"الموظف" '
                'ومقالش المندوب، اذكرها كمان في "areas_for_improvement".\n\n'
                + "\n\n".join(reference_parts)
            )

        clients = []
        if GEMINI_API_KEY:
            clients.append(_make_eval_client())
        if os.getenv("GROQ_API_KEY"):
            clients.append(
                (
                    AsyncOpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1"),
                    GROQ_EVAL_MODEL,
                )
            )
        resp = None
        last_err: Exception | None = None
        for client, eval_model in clients:
            try:
                resp = await client.chat.completions.create(
                    model=eval_model,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": eval_system_prompt},
                        {"role": "user", "content": "Transcript:\n" + "\n".join(lines)},
                    ],
                )
                break
            except Exception as e:
                last_err = e
                logger.warning("Evaluation with %s failed (%s); trying next provider", eval_model, e)
            finally:
                await client.close()
        if resp is None:
            raise last_err or RuntimeError("No evaluation provider available")

        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        criteria_result, overall_score = personas.compute_weighted_result(checklist, data.get("criteria", []))
        return {
            **base_result,
            "criteria": criteria_result,
            "overall_score": overall_score,
            "checklist_version": checklist_version,
            "strengths": data.get("strengths", []),
            "areas_for_improvement": data.get("areas_for_improvement", []),
            "coaching": data.get("coaching", ""),
            "next_step_action": data.get("next_step_action", ""),
            "lead_status": data.get("lead_status", ""),
            "lead_status_reason": data.get("lead_status_reason", ""),
        }

    async def _post_result(self, result: dict) -> None:
        headers = {"Content-Type": "application/json"}
        if WEBHOOK_SECRET:
            headers["Authorization"] = f"Bearer {WEBHOOK_SECRET}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                WEBHOOK_URL, json={"room": self.room_name, "result": result}, headers=headers
            )
            response.raise_for_status()
            logger.info("Result POSTed for room %s (HTTP %s)", self.room_name, response.status_code)


def _daily_pricing_refresh_loop() -> None:
    """يحدّث cache أسعار دفترة كل يوم الساعة 8 صباحًا بتوقيت القاهرة، لكل الدول
    المعروفة في daftra_pricing.KNOWN_COUNTRIES. يعمل مرة واحدة فورًا عند بدء
    تشغيل الـ worker (تحسبًا لعدم وجود cache خالص)، وبعدين كل 24 ساعة."""
    import threading
    import time as _time
    from datetime import datetime, timedelta

    try:
        from zoneinfo import ZoneInfo
        cairo_tz = ZoneInfo("Africa/Cairo")
    except Exception:
        cairo_tz = None

    def _refresh_all():
        for country in daftra_pricing.KNOWN_COUNTRIES:
            try:
                daftra_pricing.refresh_plans_cache(country)
            except Exception as e:
                logger.warning("فشل تحديث أسعار دفترة (%s): %s", country, e)

    def _loop():
        _refresh_all()
        while True:
            now = datetime.now(cairo_tz) if cairo_tz else datetime.utcnow()
            next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            _time.sleep(max((next_run - now).total_seconds(), 1))
            _refresh_all()

    threading.Thread(target=_loop, daemon=True, name="daftra-pricing-refresh").start()


def _test_call_link_poller_loop() -> None:
    """يفحص شيت Candidates كل 5 دقايق، ولأي مرشح حدد ميعاد اختبار (TestCallScheduledAt)
    ولسه مالوش لينك (TestCallLink)، يولّد لينك آمن ويكتبه في صف المرشح نفسه —
    بداية من ساعة قبل الميعاد. ملحوظة: لا يلمس TestCallLinkSentAt (ده مسؤولية
    Apps Script خارجي بيبعت الإيميل للمرشح). لو المرشح غيّر ميعاده بعد ما اتولدله
    لينك، المتوقع إن البلاتفورم التانية تمسح TestCallLink/TestCallToken وقت التأجيل
    عشان الفحص ده يعتبره مرشح جديد محتاج لينك تاني تلقائيًا."""
    import threading
    import time as _time

    def _poll_once():
        try:
            from sheets import get_sheets_client
            sheets = get_sheets_client()
            candidates = sheets.get_candidates_needing_test_link()
            for candidate in candidates:
                candidate_id = candidate.get('candidate_id', '')
                if not candidate_id:
                    continue
                link = sheets.generate_test_call_link(candidate_id, SCENARIOS_BASE_URL)
                if link:
                    logger.info("Auto-generated test call link for candidate %s", candidate_id)
        except Exception as e:
            logger.warning("Test call link poller failed: %s", e)

    def _loop():
        while True:
            _poll_once()
            _time.sleep(300)

    threading.Thread(target=_loop, daemon=True, name="test-call-link-poller").start()


_daily_pricing_refresh_loop()
_test_call_link_poller_loop()


server = AgentServer(
    job_executor_type=JobExecutorType.THREAD,
    num_idle_processes=0,
    # When the worker stops (deploy/restart) it drains: running calls keep
    # going, then each job gets entrypoint (15s) + on_session_end (300s) to
    # finish. Don't let the executor be killed before that.
    shutdown_process_timeout=330,
)

# job id → starts (or returns) that call's finalization task; see entrypoint.
_FINALIZERS: dict[str, Any] = {}

# Said when every LLM failed for a turn, instead of going silent.
LLM_APOLOGY_LINES = {
    "saudi": "معليش ما سمعتك زين، ممكن تعيد كلامك؟",
    "egyptian": "معلش مسمعتكش كويس، ممكن تعيد تاني؟",
    "default": "معذرة، لم أسمعك جيدًا، ممكن تعيد كلامك؟",
}


async def _on_session_end(ctx: JobContext) -> None:
    """Runs after the entrypoint, even when it was cancelled by a shutdown —
    guarantees every call gets finalized (see FINALIZATION in entrypoint)."""
    start_finalize = _FINALIZERS.pop(ctx.job.id, None)
    if start_finalize is None:
        return
    try:
        await start_finalize()
    except Exception:
        logger.exception("Finalization failed for room %s", ctx.room.name)


@server.rtc_session(agent_name=AGENT_NAME, on_session_end=_on_session_end)
async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}
    # اتصال صريح بالغرفة أول حاجة، عشان نضمن إن local_participant جاهز
    # قبل ما on_enter يحاول يبعت إشارة الرنة (بدل الاعتماد على اتصال ضمني
    # بيحصل جوه session.start() بتوقيت غير مضمون).
    await ctx.connect()
    scenario = await _resolve_scenario(ctx)

    # Parse metadata
    username = ""
    industry = ""
    test_session_id = ""
    candidate_id = ""
    candidate_email = ""
    candidate_name = ""
    user_type = "INTERNAL"
    callback_url = ""
    call_type = "AUDIO_CALL"
    attempt_id = ""
    call_id = ""
    try:
        metadata = json.loads(ctx.job.metadata or "{}")
        username = metadata.get("username", "")
        industry = metadata.get("industry", "")
        test_session_id = metadata.get("test_session_id", "")
        candidate_id = metadata.get("candidate_id", "")
        candidate_email = metadata.get("candidate_email", "")
        candidate_name = metadata.get("candidate_name", "")
        user_type = metadata.get("user_type", "INTERNAL")
        callback_url = metadata.get("callback_url", "")
        call_type = metadata.get("call_type", "AUDIO_CALL")
        attempt_id = metadata.get("attempt_id", "")
        call_id = metadata.get("call_id", "")
    except Exception:
        pass

    logger.info("Agent dispatched to %s for scenario %s (user=%s, industry=%s, user_type=%s, call_type=%s)",
               ctx.room.name, scenario["id"], username, industry, user_type, call_type)

    # Load VAD lazily
    vad = silero.VAD.load()

    session = AgentSession(
        stt=_make_stt(),
        llm=_make_llm(max_completion_tokens=GROQ_LLM_MAX_TOKENS),
        tts=_make_tts(scenario),
        vad=vad,
        turn_handling=TurnHandlingOptions(
            preemptive_generation={"preemptive_tts": True},
            interruption={"enabled": True},
            endpointing={"min_delay": 0.2, "max_delay": 1.0},
            user_turn_limit={"max_words": 300},
        ),
    )

    agent = CustomerAgent(
        scenario, ctx.room.name, room=ctx.room,
        username=username, industry=industry,
        test_session_id=test_session_id,
        candidate_id=candidate_id, candidate_email=candidate_email,
        candidate_name=candidate_name,
        user_type=user_type, callback_url=callback_url,
        call_type=call_type, attempt_id=attempt_id, call_id=call_id,
    )

    @session.on("conversation_item_added")
    def _on_item(item: Any) -> None:
        agent.record_item(item)

    # ============================================================
    # FINALIZATION (recording → Drive → evaluation → result + Calls sheet)
    # Runs exactly once per call, and must finish even when the job is being
    # shut down: LiveKit gives the entrypoint only 15s after a shutdown starts
    # (room closed, agent force-disconnected, worker draining) and then cancels
    # it — that is how calls used to get stuck "in progress". So finalization
    # is its own task: the entrypoint awaits it shielded, and _on_session_end
    # (which LiveKit runs after the entrypoint, with a 300s budget) awaits the
    # same task, starting it if the call never reached a normal end.
    # ============================================================
    async def _finalize() -> None:
        try:
            await _finalize_steps()
        finally:
            # Whatever happened above (empty transcript, evaluation crash,
            # Sheets hiccup), the call must not stay "in progress" forever.
            # No-op when the evaluation already marked it completed.
            try:
                from sheets import get_sheets_client
                closed = await asyncio.to_thread(
                    get_sheets_client().close_call_if_open, ctx.room.name, "failed")
                if closed:
                    logger.error("Call %s had no result — marked failed in Calls sheet", ctx.room.name)
            except Exception as e:
                logger.warning("Could not close call %s in Calls sheet: %s", ctx.room.name, e)

    async def _finalize_steps() -> None:
        # on_exit() normally saved the transcript already; after a forced
        # shutdown it may not have run, and saving twice is harmless.
        agent._save_call_data()
        logger.info("SESSION: call ended — beginning finalization")

        # Grace period: wait for agent's last TTS output to finish playing
        await asyncio.sleep(2.0)

        # ============================================================
        # LOCAL AUDIO RECORDING
        # RecorderIO writes to ctx.session_directory/audio.ogg as part of the
        # session's own teardown (which races with our call_ended signal), so
        # poll briefly until the file exists and its size stops growing before
        # copying it into the call's permanent local folder.
        # ============================================================
        audio_path = None
        video_path = None  # video recording is no longer captured
        call_dir = agent._get_call_dir()
        os.makedirs(call_dir, exist_ok=True)

        recorder_path = ctx.session_directory / "audio.ogg"
        last_size = -1
        for _ in range(25):  # up to ~5s
            if recorder_path.exists():
                size = recorder_path.stat().st_size
                if size > 0 and size == last_size:
                    break
                last_size = size
            await asyncio.sleep(0.2)

        if recorder_path.exists() and recorder_path.stat().st_size > 0:
            try:
                audio_path = os.path.join(call_dir, "audio.ogg")
                shutil.copyfile(recorder_path, audio_path)
                logger.info("Local audio recording saved: %s (%d bytes)", audio_path, os.path.getsize(audio_path))
            except Exception as e:
                logger.error("Failed to save local recording: %s", e)
                audio_path = None
        else:
            logger.error("Local audio recording not found at %s", recorder_path)

        # ============================================================
        # UPLOAD TO GOOGLE DRIVE
        # ============================================================
        audio_recording_obj = None
        video_recording_obj = None  # video is no longer recorded
        folder_id = ""

        try:
            from drive_upload import get_drive_client, find_or_create_folder, upload_audio, FOLDER_NAME
            drive = get_drive_client()
            call_dir = agent._get_call_dir()
            candidate_folder_name = f"{candidate_name or username or 'unknown'} - {candidate_id}" if candidate_id else (username or "unknown")

            # Create folder structure: HiringFlow AI Simulator / Candidate Name - CandidateID
            candidate_folder_id = find_or_create_folder(drive, candidate_folder_name, parent_name=FOLDER_NAME)
            if candidate_folder_id and attempt_id:
                attempt_folder_id = find_or_create_folder(drive, attempt_id, parent_name=candidate_folder_name)
                if attempt_folder_id:
                    folder_id = attempt_folder_id
                else:
                    folder_id = candidate_folder_id
            else:
                folder_id = candidate_folder_id

            # Upload audio and video independently
            if audio_path and os.path.exists(audio_path):
                audio_file_id = upload_audio(drive, audio_path, folder_id or candidate_folder_id)
                if audio_file_id:
                    audio_url = f"https://drive.google.com/file/d/{audio_file_id}/view"
                    audio_recording_obj = {
                        "status": "uploaded",
                        "file_id": audio_file_id,
                        "url": audio_url,
                    }
                    logger.info("Audio uploaded: file_id=%s url=%s", audio_file_id, audio_url)
                else:
                    audio_recording_obj = {"status": "failed", "file_id": "", "url": "", "error": "Upload failed"}
                    logger.error("Audio upload failed")

        except Exception as e:
            logger.error("Drive upload error: %s", e)

        # ============================================================
        # EVALUATION: transcript is finalized, recording is done
        # ============================================================
        logger.info("TRANSCRIPT FINALIZED: %d turns", len(agent.transcript))
        if len(agent.transcript) == 0:
            logger.error("EVALUATION BLOCKED: transcript is empty — evaluation skipped")
            logger.error("Candidate result will NOT be written to Sheets (infrastructure failure)")
        else:
            # Build recording data for webhook
            recording_data = {
                "drive_folder_id": folder_id or "",
                "audio_recording": audio_recording_obj,
                "video_recording": video_recording_obj,
            }
            await agent.evaluate_and_report(recording_data)
            logger.info("EVALUATION GENERATED: posting result")

        # ملحوظة: مفيش cleanup هنا عن قصد — ملف الصوت المحلي (audio_path) في
        # call_dir لازم يفضل محفوظ على الجهاز بشكل دائم، مش ملف مؤقت للرفع بس.

    finalize_task: asyncio.Task | None = None

    def _start_finalize() -> asyncio.Task:
        nonlocal finalize_task
        if finalize_task is None:
            finalize_task = asyncio.create_task(_finalize(), name=f"finalize-{ctx.room.name}")
        return finalize_task

    _FINALIZERS[ctx.job.id] = _start_finalize

    # لو كل نماذج اللغة فشلت في دور واحد، العميل كان بيسكت ويستنى المندوب
    # يتكلم تاني. بدل الصمت: جملة قصيرة تطلب منه يعيد كلامه.
    last_llm_apology = 0.0

    @session.on("error")
    def _on_session_error(ev: Any) -> None:
        nonlocal last_llm_apology
        if not isinstance(getattr(ev, "error", None), llm.LLMError):
            return
        now = time.monotonic()
        if now - last_llm_apology < 15:
            return
        last_llm_apology = now
        logger.warning("LLM error during call (recoverable=%s) — asking the rep to repeat",
                       getattr(ev.error, "recoverable", "?"))
        line = LLM_APOLOGY_LINES.get(scenario.get("dialect", ""), LLM_APOLOGY_LINES["default"])
        try:
            session.say(line, allow_interruptions=True, add_to_chat_ctx=False)
        except Exception as e:
            logger.warning("Could not say LLM apology line: %s", e)

    # ============================================================
    # START SESSION — connects to room AND runs the call
    # Audio recording is handled by AgentSession's built-in local
    # RecorderIO (record="audio") — no LiveKit Egress / S3 involved.
    # The call ends when on_exit() fires in CustomerAgent: the rep hung up,
    # or the max-duration timer closed the session.
    # ============================================================
    call_ended = asyncio.Event()
    agent._call_ended_event = call_ended

    logger.info("SESSION: starting — waiting for call to end")
    try:
        await session.start(agent=agent, room=ctx.room, record={"audio": True})
    except Exception as e:
        logger.warning("Session start error: %s", e)

    await call_ended.wait()
    await asyncio.shield(_start_finalize())

if __name__ == "__main__":
    import asyncio as _asyncio

    # Fetch webhook URL from Apps Script at startup - FIXED
    if HIRINGFLOW_WEBHOOK_URL:
        try:
            from hiringflow_integration import fetch_webhook_url_from_script
            try:
                _asyncio.get_running_loop()
                # Loop already running — skip scheduling, let it be handled
            except RuntimeError:
                # No running loop — create one
                _asyncio.run(fetch_webhook_url_from_script(HIRINGFLOW_WEBHOOK_URL))
        except Exception as e:
            logger.warning("Could not fetch webhook URL from Apps Script: %s", e)

    cli.run_app(server)
