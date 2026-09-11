"""LiveKit voice agent that plays the hidden customer persona during a roleplay.

Run with:  python agent.py dev

The agent connects to the LiveKit project configured in .env as a worker. When
an SDR connects to a room whose token dispatches the agent (AGENT_NAME), the
agent joins, plays the customer persona in real time, and after the call ends
it evaluates the SDR's performance and posts the result to the web server.
"""

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobProcess, cli, llm, TurnHandlingOptions
from livekit.plugins import deepgram, groq, openai
from livekit.plugins import silero
import livekit.rtc

import edge_tts_tts
import elevenlabs_tts
import gemini_tts_tts
import personas

load_dotenv()

logger = logging.getLogger("sdr-agent")
logger.setLevel(logging.INFO)


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

AGENT_NAME = os.getenv("AGENT_NAME", "sdr-training-agent")
WEBHOOK_URL = os.getenv("EVALUATION_WEBHOOK_URL", "http://localhost:8000/api/results")
WEBHOOK_SECRET = os.getenv("AGENT_WEBHOOK_SECRET", "")
SCENARIOS_BASE_URL = os.getenv("SCENARIOS_BASE_URL", "http://localhost:8000").rstrip("/")

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

    if scenario_id in personas.SCENARIOS:
        return personas.SCENARIOS[scenario_id]
    return list(personas.SCENARIOS.values())[0]


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
        logger.info("TTS: ElevenLabs (أصوات سعودية)")
        return elevenlabs_tts.ElevenLabsTTS(voice=scenario.get("voice"))
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


def _patch_recorder_mp3(session, output_path: str) -> None:
    """يعمل monkey-patch لـRecorderIO ليحفظ التسجيل كـMP3 بدل OGG."""
    import av as _av
    import numpy as _np
    from livekit.agents.voice.recorder_io import recorder_io as _rec
    import threading as _threading
    import queue as _queue
    import contextlib as _ctx
    from livekit.agents import utils as _utils

    recorder = session._recorder_io
    if recorder is None:
        return

    recorder._output_path = __import__("pathlib").Path(output_path)
    recorder._output_path.parent.mkdir(parents=True, exist_ok=True)

    _orig_encode = recorder._encode_thread.__func__

    def _mp3_encode_thread(self):
        GROW_FACTOR = 1.5
        INV_INT16 = 1.0 / 32768.0
        output_path = self._output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        container = _av.open(str(output_path), mode="w", format="mp3")
        stream = container.add_stream("libmp3lame", rate=self._sample_rate, layout="stereo")
        stream.codec_context.options["b:a"] = "128k"
        in_resampler = None
        out_resampler = None
        capacity = self._sample_rate * 6
        stereo_buf = _np.zeros((2, capacity), dtype=_np.float32)

        def remix_and_resample(frames, channel_idx):
            total_samples = sum(f.samples_per_channel * f.num_channels for f in frames)
            nonlocal capacity, stereo_buf
            if total_samples > capacity:
                while capacity < total_samples:
                    capacity = int(capacity * GROW_FACTOR)
                stereo_buf.resize((2, capacity), refcheck=False)
            pos = 0
            dest = stereo_buf[channel_idx]
            for f in frames:
                count = f.samples_per_channel * f.num_channels
                arr_i16 = _np.frombuffer(f.data, dtype=_np.int16, count=count).reshape(-1, f.num_channels)
                sl = dest[pos:pos + f.samples_per_channel]
                _np.sum(arr_i16, axis=1, dtype=_np.float32, out=sl)
                sl *= INV_INT16 / f.num_channels
                pos += f.samples_per_channel
            return pos

        try:
            with container:
                while True:
                    input_buf = self._in_q.get()
                    output_buf = self._out_q.get()
                    if input_buf is None or output_buf is None:
                        break
                    if in_resampler is None and len(input_buf):
                        in_resampler = livekit.rtc.AudioResampler(
                            input_rate=input_buf[0].sample_rate,
                            output_rate=self._sample_rate,
                            num_channels=input_buf[0].num_channels,
                        )
                    if out_resampler is None and len(output_buf):
                        out_resampler = livekit.rtc.AudioResampler(
                            input_rate=output_buf[0].sample_rate,
                            output_rate=self._sample_rate,
                            num_channels=output_buf[0].num_channels,
                        )
                    input_resampled = []
                    for frame in input_buf:
                        input_resampled.extend(in_resampler.push(frame))
                    output_resampled = []
                    for frame in output_buf:
                        output_resampled.extend(out_resampler.push(frame))
                    if output_buf:
                        output_resampled.extend(out_resampler.flush())
                    len_left = remix_and_resample(input_resampled, 0)
                    len_right = remix_and_resample(output_resampled, 1)
                    if len_left != len_right:
                        diff = abs(len_right - len_left)
                        if len_left < len_right:
                            stereo_buf[0, diff:diff + len_left] = stereo_buf[0, :len_left]
                            stereo_buf[0, :diff] = 0.0
                            len_left = len_right
                        else:
                            stereo_buf[1, diff:diff + len_right] = stereo_buf[1, :len_right]
                            stereo_buf[1, :diff] = 0.0
                            len_right = len_left
                    max_len = max(len_left, len_right)
                    if max_len <= 0:
                        continue
                    stereo_slice = stereo_buf[:, :max_len]
                    av_frame = _av.AudioFrame.from_ndarray(stereo_slice, format="fltp", layout="stereo")
                    av_frame.sample_rate = self._sample_rate
                    for packet in stream.encode(av_frame):
                        container.mux(packet)
                for packet in stream.encode(None):
                    container.mux(packet)
        except Exception:
            logger.exception("MP3 recorder encode thread failed")
        finally:
            def resolve():
                if not self._close_fut.done():
                    self._close_fut.set_result(None)
            with _ctx.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(resolve)

    recorder._encode_thread = lambda: _mp3_encode_thread(recorder)
    logger.info("RecorderIO patched to save MP3 to %s", output_path)


class CustomerAgent(Agent):
    def __init__(self, scenario: dict, room_name: str, room=None, username: str = "", industry: str = "") -> None:
        self.scenario = scenario
        self.room_name = room_name
        self.room = room
        self.username = username
        self.industry = industry
        self.started_at = time.monotonic()
        self.transcript: list[dict] = []
        self._seen_ids: set = set()
        self._reported = False
        self._call_dir: str = ""
        super().__init__(instructions=scenario["persona_prompt"])

    def _get_call_dir(self) -> str:
        """يُرجع مسار مجلد المكالمة: {username} - {Industry}"""
        if self._call_dir:
            return self._call_dir
        name = self.username or "unknown"
        ind = self.industry or "general"
        folder_name = f"{name} - {ind}"
        # إزالة الأحرف غير المسموح بها في أسماء المجلدات
        for ch in r'<>:"/\|?*':
            folder_name = folder_name.replace(ch, "_")
        self._call_dir = os.path.join(RECORDINGS_PATH, folder_name)
        return self._call_dir

    async def _send_ring_data(self) -> None:
        """يرسل رسالة data للـfrontend لتشغيل رنين التليفون."""
        if not self.room:
            return
        try:
            import json as _json
            payload = _json.dumps({"type": "ring"}).encode("utf-8")
            self.room.local_participant.publish_data(payload, topic="ring")
            logger.info("Ring data sent to frontend")
        except Exception as e:
            logger.warning("Ring data failed: %s", e)

    async def on_enter(self) -> None:
        logger.info("Call started for room %s", self.room_name)
        # ارسال رنين التليفون للـfrontend فورًا (3 حلقات = 8 ثواني)
        await self._send_ring_data()
        # انتظار حتى يخلص الرنين3 مرات +½ ثانية بعده
        await asyncio.sleep(8.5)
        await self.session.say(self.scenario["first_line"])

    async def on_exit(self) -> None:
        logger.info("Call ended for room %s (%d turns)", self.room_name, len(self.transcript))
        self._save_call_data()

    def _convert_ogg_to_mp3(self) -> None:
        """يحوّل audio.ogg إلى MP3 في مجلد المكالمة."""
        try:
            import av as _av
            call_dir = self._get_call_dir()
            ogg_path = os.path.join(call_dir, "audio.ogg")
            mp3_name = f"{self.username} - {self.industry}.mp3" if self.username and self.industry else "recording.mp3"
            for ch in r'<>:"/\|?*':
                mp3_name = mp3_name.replace(ch, "_")
            mp3_path = os.path.join(call_dir, mp3_name)
            if not os.path.exists(ogg_path):
                logger.info("No audio.ogg found at %s", ogg_path)
                return
            inp = _av.open(ogg_path)
            out = _av.open(mp3_path, mode="w", format="mp3")
            stream_out = out.add_stream("libmp3lame", rate=48000, layout="stereo")
            stream_out.codec_context.options["b:a"] = "128k"
            for frame in inp.decode(audio=0):
                for packet in stream_out.encode(frame):
                    out.mux(packet)
            for packet in stream_out.encode(None):
                out.mux(packet)
            out.close()
            inp.close()
            logger.info("Recording converted to MP3: %s", mp3_path)
        except Exception as e:
            logger.warning("OGG to MP3 conversion failed: %s", e)

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

    async def evaluate_and_report(self) -> None:
        """Evaluate the SDR once the call is over and post the result. Safe to call twice."""
        if self._reported:
            logger.info("Evaluation already reported for room %s — skipping duplicate", self.room_name)
            return
        try:
            duration = max(0, round(time.monotonic() - self.started_at))
            result = await self._evaluate(duration)
            if result:
                logger.info(
                    "Evaluation ready for room %s: score=%.1f grade=%s — posting result",
                    self.room_name, result.get("score", 0.0), result.get("grade", "?"),
                )
                # حفظ التقييم في مجلد المكالمة
                try:
                    call_dir = self._get_call_dir()
                    os.makedirs(call_dir, exist_ok=True)
                    eval_path = os.path.join(call_dir, "evaluation.json")
                    with open(eval_path, "w", encoding="utf-8") as f:
                        json.dump(result, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    logger.warning("Failed to save evaluation: %s", e)
                await self._post_result(result)
                self._reported = True
        except Exception:
            logger.exception("Evaluation failed for room %s", self.room_name)

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
                        {"role": "system", "content": personas.EVALUATION_SYSTEM_PROMPT},
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
        return {
            "scenario": self.scenario["id"],
            "scenario_name": self.scenario["name"],
            "customer_name": self.scenario["customer_name"],
            "duration_seconds": duration,
            "transcript": self.transcript,
            "scores": data.get("scores", {}),
            "overall_score": data.get("overall_score", 0),
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


server = AgentServer()


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}
    scenario = await _resolve_scenario(ctx)

    # استخراج بيانات المستخدم والنشاط من metadata
    username = ""
    industry = ""
    try:
        metadata = json.loads(ctx.job.metadata or "{}")
        username = metadata.get("username", "")
        industry = metadata.get("industry", "")
    except Exception:
        pass

    logger.info("Agent dispatched to %s for scenario %s (user=%s, industry=%s)", ctx.room.name, scenario["id"], username, industry)

    session = AgentSession(
        stt=_make_stt(),
        llm=_make_llm(max_completion_tokens=GROQ_LLM_MAX_TOKENS),
        tts=_make_tts(scenario),
        vad=ctx.proc.userdata["vad"],
        turn_handling=TurnHandlingOptions(
            preemptive_generation={"preemptive_tts": True},
            interruption={"enabled": True},
            endpointing={"min_delay": 0.2, "max_delay": 1.0},
            user_turn_limit={"max_words": 300},
        ),
    )

    agent = CustomerAgent(scenario, ctx.room.name, room=ctx.room, username=username, industry=industry)

    @session.on("conversation_item_added")
    def _on_item(item: Any) -> None:
        agent.record_item(item)

    # توجيه مجلد التسجيلات إلى المسار المحدد
    os.makedirs(RECORDINGS_PATH, exist_ok=True)
    ctx._session_directory = __import__("pathlib").Path(RECORDINGS_PATH)

    await session.start(agent=agent, room=ctx.room, record=True)
    await ctx.connect()

    # بعد انتهاء الجلسة بالكامل: انتظار الملفات ثم المعالجة
    import time as _time
    ogg_path = os.path.join(RECORDINGS_PATH, "audio.ogg")
    # انتظار حتى يكتمل ملف OGG تمامًا
    for _ in range(30):
        if os.path.exists(ogg_path) and os.path.getsize(ogg_path) > 0:
            sz1 = os.path.getsize(ogg_path)
            _time.sleep(1.0)
            sz2 = os.path.getsize(ogg_path)
            if sz1 == sz2:
                break
        _time.sleep(0.5)
    # نقل OGG إلى مجلد المكالمة
    call_dir = agent._get_call_dir()
    os.makedirs(call_dir, exist_ok=True)
    ogg_dst = os.path.join(call_dir, "audio.ogg")
    try:
        if os.path.exists(ogg_path) and not os.path.exists(ogg_dst):
            os.rename(ogg_path, ogg_dst)
    except Exception:
        pass
    # تحويل OGG → MP3
    agent._convert_ogg_to_mp3()
    # تقييم المكالمة
    await agent.evaluate_and_report()


if __name__ == "__main__":
    cli.run_app(server)
