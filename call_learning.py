"""تعلم اللهجة وبيئة العملاء الحقيقيين من مكالمة مسجلة (MP3).

التدفق: رفع MP3 → تفريغ نصي عبر Deepgram (مع فصل المتحدثين) → تحليل عبر Gemini
لاستخراج ملف شخصية العميل (اسم، دور، مجال، نقطة ألم، مستوى صعوبة، تعابير اللهجة
العامية الحقيقية، نبرة الكلام). يُحفظ الملف كسيناريو عميل مخصص جديد، وتُجمَّع
تعابير اللهجة في ملف بيانات لتطوير الأساس العامي لكل السيناريوهات.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger("sdr-agent.call_learning")

BASE_DIR = Path(__file__).resolve().parent
CALLS_DIR = BASE_DIR / "data" / "calls"
CORPUS_FILE = BASE_DIR / "data" / "dialect_corpus.json"

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
}

ANALYSIS_PROMPT = """أنت محلل مبيعات خبير. اقرأ نص مكالمة حقيقية بين مندوب مبيعات (SDR) وعميل محتمل.
المتحدثان مسمّيان S1 وS2. حدد أولًا مَن هو العميل: الطرف الذي يتلقى المكالمة، لا يبيع،
يُجيب على الأسئلة باختصار، وعادة لا يعرف المتصل. ومَن هو المندوب الذي يعرض ويقود.

ثم استخرج ملف شخصية العميل فقط (وليس المندوب):
- customer_name: اسم العميل إن ذُكر، وإلا نص فارغ
- customer_role: دوره/منصبه
- company_name: اسم شركته إن ذُكر
- business_field: مجال عمله (توزيع، صيدلية، مقاولات...)
- pain_point: نقطة الألم أو المشكلة الأساسية التي ذكرها العميل بجملة واحدة واضحة
- budget_sensitivity: منخفضة / متوسطة / عالية
- buying_intent: منخفضة / متوسطة / عالية
- difficulty: easy أو medium أو hard حسب تحفظه واعتراضاته
- tone: وصف نبرته وطريقة كلامه في سطر واحد
- dialect_phrases: قائمة من 12 إلى 20 عبارة أو جملة قالها العميل فعليًا بكلماته،
  سعودية عامية صريحة، لتكون أمثلة حقيقية تُقتبس في ردود العميل الذكي
- dialect_notes: وصف لهجته (كلمات وتعبيرات وأسلوب) في سطرين
- objections: قائمة الاعتراضات التي أبداها العميل

أعد JSON صارمًا فقط (بدون markdown) بهذا الشكل تمامًا:
{"customer_name": "", "customer_role": "", "company_name": "", "business_field": "",
 "pain_point": "", "budget_sensitivity": "", "buying_intent": "", "difficulty": "",
 "tone": "", "dialect_phrases": [], "dialect_notes": "", "objections": []}"""


def _deepgram_key() -> str:
    return os.getenv("DEEPGRAM_API_KEY", "").strip()


async def transcribe_audio(path: Path) -> list[dict]:
    """يفرّغ مكالمة مسجلة إلى نص مع فصل المتحدثين.

    يُرجع قائمة: [{"speaker": 0|1, "text": "..."}, ...]
    """
    key = _deepgram_key()
    if not key:
        raise RuntimeError("DEEPGRAM_API_KEY غير مضبوطة في .env")
    params = {
        "model": "nova-3",
        "language": "ar",
        "smart_format": "true",
        "punctuate": "true",
        "diarize": "true",
        "utterances": "true",
    }
    headers = {"Authorization": f"Token {key}"}
    ctype = CONTENT_TYPES.get(path.suffix.lower())
    if ctype:
        headers["Content-Type"] = ctype
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            DEEPGRAM_URL, params=params, headers=headers, content=path.read_bytes()
        )
        resp.raise_for_status()
        data = resp.json()
    utterances = (data.get("results") or {}).get("utterances") or []
    return [
        {"speaker": u.get("speaker", 0), "text": (u.get("transcript") or "").strip()}
        for u in utterances
        if u.get("transcript")
    ]


async def analyze_transcript(utterances: list[dict], api_key: str, model: str) -> dict:
    """يحلّل نص المكالمة عبر Gemini ويستخرج ملف شخصية العميل."""
    from openai import AsyncOpenAI

    lines = [f"S{u['speaker']}: {u['text']}" for u in utterances]
    client = AsyncOpenAI(api_key=api_key, base_url=GEMINI_BASE_URL)
    try:
        resp = await client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": ANALYSIS_PROMPT},
                {"role": "user", "content": "Transcript:\n" + "\n".join(lines)},
            ],
        )
    finally:
        await client.close()
    raw = resp.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except Exception:
        logger.warning("Gemini returned non-JSON analysis: %s", raw[:200])
        return {}


def profile_to_brief(profile: dict) -> dict:
    """يحوّل ملف الشخصية إلى ملف تعريف سيناريو مخصص (بما يتوقعه build_custom_scenario)."""
    name = (profile.get("customer_name") or "عميل حقيقي").strip()
    field = (profile.get("business_field") or "عمل تجاري").strip()
    difficulty = profile.get("difficulty") or "medium"
    # شخصية افتراضية حسب مستوى الصعوبة إن ما حدد المحلل شخصية صريحة.
    persona = profile.get("persona") or {"easy": "friendly", "medium": "hesitant", "hard": "difficult"}.get(difficulty, "hesitant")
    return {
        # العنوان لا يكشف اسم العميل قبل المكالمة (المتدرب يطلب الاسم بنفسه).
        "name": f"عميل من مكالمة حقيقية – {field}",
        "customer_name": name,
        "customer_role": (profile.get("customer_role") or "عميل محتمل").strip(),
        "company_name": (profile.get("company_name") or "").strip(),
        "business_field": field,
        "company_size": "",
        "pain_point": (profile.get("pain_point") or "").strip(),
        "decision_maker": True,
        "budget_sensitivity": profile.get("budget_sensitivity") or "متوسطة",
        "buying_intent": profile.get("buying_intent") or "متوسطة",
        "difficulty": difficulty,
        "product_brief": "",
        "persona": persona,
        "dialect_phrases": [p for p in (profile.get("dialect_phrases") or []) if str(p).strip()],
        "dialect_notes": (profile.get("dialect_notes") or "").strip(),
        "tone": (profile.get("tone") or "").strip(),
    }


def load_dialect_corpus() -> list[str]:
    """يقرأ تعابير اللهجة المتراكمة من المكالمات الحقيقية."""
    if not CORPUS_FILE.exists():
        return []
    try:
        data = json.loads(CORPUS_FILE.read_text(encoding="utf-8"))
        return [p for p in data if isinstance(p, str) and p.strip()]
    except Exception:
        return []


def add_to_corpus(phrases: list[str]) -> int:
    """يضيف تعابير اللهجة الجديدة إلى الكوربوس (مع منع التكرار) ويرجع عددها."""
    phrases = [p.strip() for p in phrases if p and p.strip()]
    if not phrases:
        return 0
    corpus = load_dialect_corpus()
    seen = set(corpus)
    added = 0
    for p in phrases:
        if p not in seen:
            corpus.append(p)
            seen.add(p)
            added += 1
    if added:
        CORPUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        CORPUS_FILE.write_text(json.dumps(corpus, ensure_ascii=False, indent=2), encoding="utf-8")
    return added