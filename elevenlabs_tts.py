"""TTS plugin: ElevenLabs Saudi voices (بث REST مباشر، بنفس بنية edge_tts_tts).

يستخدم أصواتًا سعودية خليجية طبيعية جدًا (نفس بث الصوت الذي يعمل به Edge، mp3 ->
PyAV). لكل جملة تُضبط إعدادات الصوت حسب مشاعر العميل المكتشفة من كلماته العامية
(stability/style/speed) — فلا يحتاج أي مفتاح إضافي غير ELEVEN_API_KEY.

إن لم يُحدَّد المفتاح يبقى النظام كما هو (Edge المجاني)؛ الاختيار يتم في agent.py.
"""

from __future__ import annotations

import logging
import os
import random

import httpx

from livekit.agents import tts, utils
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

logger = logging.getLogger("sdr-agent.elevenlabs")

API_URL = "https://api.elevenlabs.io/v1/text-to-speech"
# turbo_v2_5 أسرع بنسبة ~30-40% من multilingual_v2 (قِيس فعليًا) بجودة عربي قريبة جدًا،
# ومصمم لمحادثات الوقت الفعلي. لو محتاج أسرع من كده على حساب جودة أبسط: eleven_flash_v2_5
MODEL = "eleven_turbo_v2_5"
OUTPUT_FORMAT = "mp3_44100_128"
SAMPLE_RATE = 44100
NUM_CHANNELS = 1

# لهجة العميل -> صوت ElevenLabs سعودي (ذكر/أنثى). قابلة للضبط من المتغيرات:
#   ELEVEN_VOICE_SAUDI / ELEVEN_VOICE_SAUDI_MALE / ELEVEN_VOICE_SAUDI_FEMALE
DEFAULT_VOICES = {
    "saudi": "balRgnGuyobFvldHuixQ",        # Taba – صوت سعودي
    "saudi-male": "balRgnGuyobFvldHuixQ",
    "saudi-female": "E4GutuQ39akNBbiYuhh2",  # Heba Mansuri – سعودية بنبرة حازمة
}
DEFAULT_VOICE = DEFAULT_VOICES["saudi"]

# أصوات مصنّفة حسب اللهجة (اتسحبت من ElevenLabs) — تُستخدم للاختيار العشوائي لما
# العميل الذكي مالوش صوت محدد بنفسه. الأصوات اللي لسه لهجتها مش معروفة (Omar Osama،
# Hessin Abdallah، meshary) عمدًا مش داخلة هنا لحد ما نتأكد منها.
SAUDI_VOICE_POOL = {
    "Taba": "balRgnGuyobFvldHuixQ",
    "Moazz": "FELlZ7P5dpkH4BJsVcWt",
    "Salem Ahmed": "SzEQh89cwBQTN77VF8m7",
    "GAWALY": "vhIzf2BLcnHOQbGSOOSe",
    "Houzimi 2": "hlz7UHeM8xI2YLxXpNSY",
}
EGYPTIAN_VOICE_POOL = {
    "Azaam": "EdfmSDZWwPKvZKdHoa4A",
    "siso": "M2ZmelgiD3eutyzRXWIZ",
    "sisi": "6wEGFb9HfmAGibGevl4o",
    "fla7": "zxJ1nKbTdTEDKgscIQ5T",
}


def random_voice_for_dialect(dialect: str) -> str:
    """يختار صوت عشوائي من نفس لهجة السيناريو (سعودي/مصري)."""
    pool = EGYPTIAN_VOICE_POOL if dialect == "egyptian" else SAUDI_VOICE_POOL
    return random.choice(list(pool.values()))


def _env_voice(key: str, fallback: str) -> str:
    return (os.getenv(key) or "").strip() or fallback


def _voice_settings(text: str) -> dict:
    """يُقدّر مشاعر العميل من كلماته العامية ويبني إعدادات الصوت المناسبة."""
    if any(k in text for k in [
        "مستحيل", "متضايق", "زعلان", "مو معقول", "وش ذا الكلام", "ماعندي وقت",
        "تعبت", "لا أبد", "مزعج", "معليش بس", "تستعجل", "شكلك", "طيب خلاص",
        "بس خلاص", "زعلت", "أنا منزعج", "وش جابك", "مو بهالسرعة",
    ]):
        return {"stability": 0.25, "similarity_boost": 0.8, "style": 0.85, "use_speaker_boost": True, "speed": 1.1}
    if any(k in text for k in [
        "مب متأكد", "خايف", "نبي نشوف", "طيب بس", "مب واضح", "جربنا قبل",
        "وش الفايدة", "بس وش", "نشوف", "ليش", "مو مقتنع", "أنا شاك", "يعني إيش",
        "ما ندري", "وش ربحه",
    ]):
        return {"stability": 0.6, "similarity_boost": 0.8, "style": 0.45, "use_speaker_boost": True, "speed": 0.95}
    if any(k in text for k in [
        "متحمس", "يا سلام", "مو مصدق", "والله حلو", "خلنا نبدأ", "على طول",
        "حلو مره", "رائع", "ممتاز", "عجبتني الفكرة", "الله يوفقك",
    ]):
        return {"stability": 0.2, "similarity_boost": 0.8, "style": 1.0, "use_speaker_boost": True, "speed": 1.18}
    if any(k in text for k in [
        "مستعجل", "ما عندي وقت", "بدري", "نبي نخلص", "الحين الحين", "أنا مستعجل",
        "وقتنا ضيق", "بس نخلصها بسرعة",
    ]):
        return {"stability": 0.35, "similarity_boost": 0.8, "style": 0.65, "use_speaker_boost": True, "speed": 1.2}
    if any(k in text for k in [
        "الله يعطيك العافية", "زين", "حلو", "عجبني", "طيب تمام", "بساعدك",
        "ما شاء الله", "خلنا نتكلم", "عساك بخير", "هلا والله", "بخير الحمد لله",
    ]):
        return {"stability": 0.45, "similarity_boost": 0.8, "style": 0.75, "use_speaker_boost": True, "speed": 1.08}
    return {"stability": 0.5, "similarity_boost": 0.75, "style": 0.3, "use_speaker_boost": True, "speed": 1.0}


def _resolve_voice(voice: str | None) -> str:
    """حل معرّف الصوت: رقم ElevenLabs مباشرة، أو معرّف Edge يُترجم حسب الجنس."""
    if voice:
        if voice.startswith("ar-") or voice.endswith("Neural"):
            if any(g in voice for g in ("Zariyah", "Female", "female")):
                return _env_voice("ELEVEN_VOICE_SAUDI_FEMALE", DEFAULT_VOICES["saudi-female"])
            return _env_voice("ELEVEN_VOICE_SAUDI_MALE", DEFAULT_VOICES["saudi-male"])
        return voice
    return _env_voice("ELEVEN_VOICE_SAUDI", DEFAULT_VOICE)


class ElevenLabsTTS(tts.TTS):
    def __init__(self, *, voice: str | None = None, api_key: str | None = None) -> None:
        self._voice = _resolve_voice(voice)
        self._api_key = api_key or os.getenv("ELEVEN_API_KEY") or ""
        self._client: httpx.AsyncClient | None = None
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
        )

    @property
    def model(self) -> str:
        return f"elevenlabs/{MODEL}"

    @property
    def provider(self) -> str:
        return "ElevenLabs"

    def _ensure_client(self) -> httpx.AsyncClient:
        # عميل httpx واحد يُعاد استخدامه بدل إنشاء عميل جديد (وسياق SSL جديد يوقف
        # الـ event loop ~0.3-0.4 ثانية) مع كل جملة يقولها العميل الذكي.
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await super().aclose()

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
        tts: ElevenLabsTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: ElevenLabsTTS = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            mime_type="audio/mpeg",
        )
        if not self._tts._api_key:
            raise RuntimeError("ELEVEN_API_KEY غير محدد — لا يمكن استخدام ElevenLabs")
        settings = _voice_settings(self._input_text)
        speed = settings.pop("speed")
        payload = {
            "text": self._input_text,
            "model_id": MODEL,
            "voice_settings": settings,
            "speed": speed,
        }
        url = f"{API_URL}/{self._tts._voice}/stream?output_format={OUTPUT_FORMAT}"
        headers = {"xi-api-key": self._tts._api_key, "Content-Type": "application/json"}
        try:
            client = self._tts._ensure_client()
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise RuntimeError(f"ElevenLabs HTTP {resp.status_code}: {body[:200]}")
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        output_emitter.push(chunk)
        except Exception as e:
            logger.warning("ElevenLabs TTS failed for %s: %s", self._tts._voice, e)
            raise
        finally:
            output_emitter.flush()