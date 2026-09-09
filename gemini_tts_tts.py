"""TTS plugin: Gemini 2.5 TTS (preview) — أصوات عربية طبيعية جدًا بمشاعر.

يستخدم نموذج `gemini-2.5-flash-preview-tts` (أو pro) عبر Gemini API بنفس مفتاح
GEMINI_API_KEY — لا حاجة لأي اشتراك ElevenLabs. يتبع النموذج التعليمات النصية
للنبرة والمشاعر، فيخرج صوت إنسان طبيعي (مممم، تردد، حماس، غضب).

يتطلب اتصالًا بالإنترنت فقط. الحساب المجاني يحمل حدودًا؛ حساب Pro يرفعها.

العائد: PCM خام (L16) بمعدل 24000Hz — نغلّفه بترويسة WAV لأن LiveKit يفك تشفيرها
بسهولة عبر PyAV.
"""

from __future__ import annotations

import base64
import json
import logging
import struct

import httpx

from livekit.agents import tts, utils
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

logger = logging.getLogger("sdr-agent.gemini_tts")

SAMPLE_RATE = 24000
NUM_CHANNELS = 1
_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# مشاعر -> وصف صوتي بالإنجليزية يُدرج في تعليمات النموذج (يقرأها النموذج لضبط النبرة).
_EMOTION_PROMPTS: list[tuple[list[str], str]] = [
    (
        ["مستحيل", "متضايق", "ماعندي وقت", "بس خلاص", "زعلت", "وش ذا الكلام", "مزعج", "تعبت"],
        "slightly irritated, a bit short and dismissive, firmer tone, not shouting",
    ),
    (
        ["مب متأكد", "نشوف", "مو مقتنع", "خايف", "أول مرة أسمع", "بس وش", "مدري", "يعني إيه"],
        "hesitant, uncertain, thinking while talking, a bit skeptical, with small thinking pauses",
    ),
    (
        ["يا سلام", "والله حلو", "ممتاز", "عجبتني", "متحمس", "خلنا نبدأ", "حلو مره"],
        "happy, warm, slightly excited, brighter tone, friendly",
    ),
    (
        ["مستعجل", "معلش بس", "وقتنا ضيق", "بدري", "نبي نخلص"],
        "in a hurry, quick, a bit impatient, faster rhythm",
    ),
]

_GREETING_PREFIXES = ("الو", "ألو", "آلو", "هلا", "صباح", "السلام", "هاي", "إزيك", "عامل")


def _voice_instruction(text: str) -> str:
    base = (
        "a middle-aged Egyptian man with a natural warm telephone voice, "
        "casual Egyptian dialect, conversational and expressive with real emotions"
    )
    stripped = text.lstrip()
    if stripped.startswith(_GREETING_PREFIXES):
        base += (
            ", like someone who just picked up the phone and is just starting "
            "to talk, slightly slower and softer"
        )
    for keywords, emo in _EMOTION_PROMPTS:
        if any(k in text for k in keywords):
            base += ", " + emo
            break
    return base


def _wav_header(pcm_len: int, sample_rate: int = SAMPLE_RATE, channels: int = NUM_CHANNELS) -> bytes:
    bits = 16
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + pcm_len,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data",
        pcm_len,
    )


def _wrap_wav(pcm: bytes) -> bytes:
    return _wav_header(len(pcm)) + pcm


class GeminiTTS(tts.TTS):
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-2.5-flash-preview-tts",
    ) -> None:
        self._api_key = api_key
        self._model = model
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
        )

    @property
    def model(self) -> str:
        return f"gemini-tts/{self._model}"

    @property
    def provider(self) -> str:
        return "GeminiTTS"

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
        tts: GeminiTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: GeminiTTS = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            mime_type="audio/wav",  # LiveKit يفك تشفير WAV إلى PCM عبر PyAV
        )
        instruction = _voice_instruction(self._input_text)
        prompt = (
            "Speak ONLY this exact Arabic text aloud, nothing else. Do not translate, "
            "do not add, remove or change any word. " + instruction + "\n\nText: " + self._input_text
        )
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["AUDIO"]},
        }
        url = f"{_BASE_URL}/{self._tts._model}:generateContent"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, params={"key": self._tts._api_key}, json=body)
            resp.raise_for_status()
            data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Gemini TTS error: {data['error']}")
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError("Gemini TTS returned no audio") from exc
        pcm = b""
        for part in parts:
            inline = part.get("inlineData") or {}
            if inline.get("data"):
                pcm = base64.b64decode(inline["data"])
                break
        if not pcm:
            raise RuntimeError("Gemini TTS returned empty audio")
        output_emitter.push(_wrap_wav(pcm))
        output_emitter.flush()