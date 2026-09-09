"""المصادقة والأدوار لمعمل تدريب المبيعات.

المستخدمون مخزنون في data/users.json (بكلمة مرور مشفرة PBKDF2).
الرموز (tokens) هي JWT — تبقى سارية حتى إعادة التشغيل.

الأدوار:
- sdr:      مندوب مبيعات — مكالمة عشوائية فقط
- quality:  فريق الجودة — يختار العملاء المحددين
- admin:    مدير — كل الصلاحيات
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path

import jwt

BASE_DIR = Path(__file__).resolve().parent
USERS_FILE = BASE_DIR / "data" / "users.json"

JWT_SECRET = os.getenv("JWT_SECRET", secrets.token_urlsafe(32))
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 720  # 30 يوم

ROLES = {
    "sdr": "مندوب مبيعات",
    "quality": "فريق الجودة",
    "admin": "مدير",
}

DEFAULT_USERS = [
    ("admin", "admin123", "مدير", "admin"),
    ("sdr", "sdr123", "مندوب تجريبي", "sdr"),
    ("quality", "quality123", "مراقبة جودة", "quality"),
]


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
    return f"{salt}${dk}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(_hash_password(password, salt), stored)


def _write(users: dict) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")


def seed() -> None:
    """ينشئ ملف المستخدمين بقيم افتراضية عند أول تشغيل فقط."""
    if USERS_FILE.exists():
        return
    admin_pw = os.getenv("ADMIN_PASSWORD", "admin123").strip() or "admin123"
    users: dict = {}
    for username, password, name, role in DEFAULT_USERS:
        users[username] = {
            "password": _hash_password(admin_pw if username == "admin" else password),
            "name": name,
            "role": role,
        }
    _write(users)
    print(f"[auth] تم إنشاء المستخدمين الافتراضيين. admin / {admin_pw} — غيّر كلمة المرور من لوحة المدير.")


def load_users() -> dict:
    if not USERS_FILE.exists():
        seed()
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def public_user(username: str, record: dict) -> dict:
    return {
        "username": username,
        "name": (record.get("name") or username),
        "role": (record.get("role") if record.get("role") in ROLES else "sdr"),
    }


def _create_token(username: str) -> str:
    """ينشئ رمز JWT ينتهي بعد JWT_EXPIRY_HOURS ساعة."""
    payload = {
        "sub": username,
        "iat": int(time.time()),
        "exp": int(time.time()) + (JWT_EXPIRY_HOURS * 3600),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> str | None:
    """يفك رمز JWT ويرجع اسم المستخدم أو None."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def authenticate(username: str, password: str) -> dict | None:
    """يُصادق ويرجع {"token", "user"} أو None."""
    users = load_users()
    record = users.get(username)
    if not record or not _verify_password(password, record.get("password", "")):
        return None
    token = _create_token(username)
    return {"token": token, "user": public_user(username, record)}


def user_from_token(token: str | None) -> dict | None:
    if not token:
        return None
    # دعم الرموز القديمة (in-memory) — إذا فشل JWT، جرّب البحث في الملف
    username = _decode_token(token)
    if not username:
        return None
    record = load_users().get(username)
    if not record:
        return None
    return public_user(username, record)


def logout(token: str | None) -> None:
    # JWT tokens تنتهي تلقائيًا — لا حاجة لحذفها
    pass


def list_users() -> list[dict]:
    return [public_user(name, rec) for name, rec in load_users().items()]


def upsert_user(username: str, name: str, role: str, password: str | None = None) -> dict | None:
    username = username.strip()
    if not username:
        return None
    role = role if role in ROLES else "sdr"
    users = load_users()
    record = users.get(username, {})
    if password:
        record["password"] = _hash_password(password)
    record["name"] = (name or username).strip()
    record["role"] = role
    users[username] = record
    _write(users)
    return public_user(username, record)


def delete_user(username: str) -> bool:
    if username == "admin":
        return False
    users = load_users()
    if username not in users:
        return False
    del users[username]
    _write(users)
    return True


def generate_credentials(base_username: str, names: list[str], role: str = "sdr") -> list[dict]:
    """ينشئ عدة حسابات دفعة واحدة بأسماء مستخدمين تلقائية وكلمات مرور عشوائية."""
    role = role if role in ROLES else "sdr"
    users = load_users()
    created: list[dict] = []
    index = 1
    for raw in names:
        name = (raw or "").strip()
        if not name:
            continue
        while True:
            candidate = f"{base_username}{index}"
            index += 1
            if candidate not in users:
                break
        password = secrets.token_hex(4)
        users[candidate] = {
            "password": _hash_password(password),
            "name": name,
            "role": role,
        }
        created.append({"username": candidate, "name": name, "role": role, "password": password})
    if created:
        _write(users)
    return created
