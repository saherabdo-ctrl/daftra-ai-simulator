"""TTS plugin: Microsoft Edge neural voices via the free `edge-tts` service.

أصوات Microsoft Edge مجانية تمامًا بلهجات عربية طبيعية:

- ar-SA-HamedNeural    (سعودي، ذكر)
- ar-SA-ZariyahNeural  (سعودية)
- ar-EG-ShakirNeural   (مصري، ذكر)
- ar-EG-SalmaNeural    (مصرية)

لا يحتاج مفتاح API، يتطلب اتصالًا بالإنترنت فقط.

المشاعر في الصوت: نُقدّر مشاعر العميل من كلماته العامية (كما يكتبها نموذج اللغة
وفق شخصيته وتفاعله مع المندوب) ثم نضبط سرعة/حدّة/ارتفاع الصوت لكل جملة على حدة،
فيصبح نبرة العميل ومشاعره متفاعلة مع سلوك المندوب دون أي مفتاح إضافي.
"""

from __future__ import annotations

import logging

import edge_tts

from livekit.agents import tts, utils
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

logger = logging.getLogger("sdr-agent.edge_tts")

SAMPLE_RATE = 48000
NUM_CHANNELS = 1

# لهجة العميل -> صوت Edge المناسب (ذكر/أنثى لكل عميل).
VOICES = {
    "saudi": "ar-SA-HamedNeural",
    "saudi-male": "ar-SA-HamedNeural",
    "saudi-female": "ar-SA-ZariyahNeural",
    "egyptian": "ar-EG-ShakirNeural",
    "egyptian-male": "ar-EG-ShakirNeural",
    "egyptian-female": "ar-EG-SalmaNeural",
}
DEFAULT_VOICE = "ar-SA-HamedNeural"

# نبرة الكلام الأساسية: أسرع قليلًا لردٍ أنشط وأقل تأخيرًا في سماع العميل.
BASE_RATE = "+6%"
BASE_PITCH = "+0Hz"
BASE_VOLUME = "+0%"

# بدايات الرد الأول: نعطيها نبرة «لسه ردّيت على التليفون» (أبطأ وأنعم قليلًا).
_GREETING_PREFIXES = ("الو", "ألو", "آلو", "هلا", "صباح", "السلام", "هاي", "إزيك", "عامل")

# حالات المشاعر: (مفتاح، إعدادات نطق، كلمات عامية تدل عليها) بالترتيب الأكثر تحديدًا أولًا.
_EMOTIONS: list[tuple[str, dict, list[str]]] = [
    (
        "angry",
        {"rate": "+8%", "pitch": "-12Hz", "volume": "+5%"},
        [
            "مستحيل", "متضايق", "زعلان", "مو معقول", "وش ذا الكلام", "ماعندي وقت",
            "تعبت", "لا أبد", "مزعج", "معليش بس", "تستعجل", "شكلك", "طيب خلاص",
            "بس خلاص", "زعلت", "من متكلم", "أنا منزعج", "وش جابك", "مو بهالسرعة",
        ],
    ),
    (
        "skeptical",
        {"rate": "-4%", "pitch": "+2Hz", "volume": "+0%"},
        [
            "مب متأكد", "خايف", "نبي نشوف", "طيب بس", "مب واضح", "جربنا قبل",
            "وش الفايدة", "بس وش", "نشوف", "ليش", "مو مقتنع", "أنا شاك", "يعني إيش",
            "حسبي", "بيجينا ناس كثير", "كلامهم كثير", "توك داق", "ولا نعرف بعض",
            "أول مرة أسمع", "وش يسوي", "واجد", "ما ندري", "وش ربحه",
        ],
    ),
    (
        "excited",
        {"rate": "+20%", "pitch": "+14Hz", "volume": "+10%"},
        [
            "متحمس", "يا سلام", "مو مصدق", "والله حلو", "خلنا نبدأ", "على طول",
            "حلو مره", "رائع", "ممتاز", "عجبتني الفكرة", "الله يوفقك",
        ],
    ),
    (
        "urgent",
        {"rate": "+14%", "pitch": "+4Hz", "volume": "+3%"},
        [
            "مستعجل", "ما عندي وقت", "بدري", "نبي نخلص", "الحين الحين", "أنا مستعجل",
            "وقتنا ضيق", "بس نخلصها بسرعة",
        ],
    ),
    (
        "happy",
        {"rate": "+14%", "pitch": "+8Hz", "volume": "+8%"},
        [
            "الله يعطيك العافية", "زين", "حلو", "عجبني", "طيب تمام", "بساعدك",
            "يعطيك العافية", "ما شاء الله", "خلنا نتكلم", "عساك بخير", "هلا والله",
            "وش أخبارك", "بخير الحمد لله", "يعجبني",
        ],
    ),
]


def _emotion_settings(text: str) -> dict:
    """يُقدّر مشاعر العميل من كلماته العامية ويعيد إعدادات النطق المناسبة (أو فارغًا)."""
    stripped = text.lstrip()
    # لقط الرد الأول: نبرة «حد لسه راد على التليفون» — أبطأ وأنعم قليلًا (زي «الو؟» طبيعية).
    if stripped.startswith(_GREETING_PREFIXES):
        return {"rate": "-6%", "pitch": "-6Hz", "volume": "+0%"}
    for _label, settings, keywords in _EMOTIONS:
        if any(k in text for k in keywords):
            return settings
    return {}


def _tts_params(text: str) -> dict:
    settings = _emotion_settings(text)
    return {
        "rate": settings.get("rate", BASE_RATE),
        "pitch": settings.get("pitch", BASE_PITCH),
        "volume": settings.get("volume", BASE_VOLUME),
    }


class EdgeTTS(tts.TTS):
    def __init__(self, *, voice: str | None = None) -> None:
        self._voice = voice or DEFAULT_VOICE
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
        )

    @property
    def model(self) -> str:
        return f"edge-tts/{self._voice}"

    @property
    def provider(self) -> str:
        return "EdgeTTS"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> ChunkedStream:
        return ChunkedStream(tts=self, input_text=text, conn_options=conn_options)


class ChunkedStream(tts.ChunkedStream):
    def __init__(
        self,
        *,
        tts: EdgeTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: EdgeTTS = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            mime_type="audio/mpeg",  # LiveKit يفك تشفير mp3 إلى PCM عبر PyAV
        )
        params = _tts_params(self._input_text)
        try:
            communicate = edge_tts.Communicate(
                self._input_text,
                self._tts._voice,
                rate=params["rate"],
                pitch=params["pitch"],
                volume=params["volume"],
            )
            async for chunk in communicate.stream():
                if chunk["type"] != "audio":
                    continue
                data = chunk["data"]
                if not data:
                    continue
                output_emitter.push(data)
        except Exception as e:
            logger.warning("Edge TTS failed for %s: %s", self._tts._voice, e)
            raise
        finally:
            output_emitter.flush()