"""Operational alerts to a chat webhook.

Set ALERT_WEBHOOK_URL to a Slack or Google Chat incoming-webhook URL (both
accept {"text": ...}); unset = alerts are off. ALERT_ENV_LABEL (e.g. "prod")
is prefixed to every message. The same alert is sent at most once every 15
minutes, so a failing provider can't flood the channel.
"""
import json
import logging
import os
import threading
import time
import urllib.request

_URL = os.getenv("ALERT_WEBHOOK_URL", "").strip()
_LABEL = os.getenv("ALERT_ENV_LABEL", "").strip()
_COOLDOWN_SECONDS = 15 * 60

_last_sent: dict[str, float] = {}
_lock = threading.Lock()
_logger = logging.getLogger("alerts")


def send_alert(text: str, key: str | None = None) -> None:
    """Post `text` to the webhook in the background (never blocks, never raises)."""
    if not _URL:
        return
    key = key or text[:80]
    now = time.monotonic()
    with _lock:
        if now - _last_sent.get(key, float("-inf")) < _COOLDOWN_SECONDS:
            return
        _last_sent[key] = now
    prefix = f"[daftra-ai-simulator{' ' + _LABEL if _LABEL else ''}]"
    threading.Thread(target=_post, args=(f"{prefix} {text}",), daemon=True).start()


def _post(text: str) -> None:
    try:
        req = urllib.request.Request(
            _URL,
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        _logger.warning("alert webhook failed: %s", e)


class AlertLogHandler(logging.Handler):
    """Turns log lines that contain a known failure phrase into alerts.

    `patterns` maps a phrase to look for in the message → the alert title."""

    def __init__(self, patterns: dict[str, str]) -> None:
        super().__init__(level=logging.WARNING)
        self.patterns = patterns

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        for phrase, title in self.patterns.items():
            if phrase in msg:
                send_alert(f"{title}\n{msg[:400]}", key=phrase)
                return
