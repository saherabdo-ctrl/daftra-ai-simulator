"""يجيب أسعار باقات دفترة الحالية من https://www.daftra.com/plans مباشرة (نفس
الموقع اللي بيشوفه أي عميل حقيقي)، لكل دولة على حدة، ويحفظها في cache محلي
عشان الـ AI Simulator يقيّم المندوب بناءً على أسعار حقيقية ومحدثة، من غير ما
كل مكالمة تعمل طلب شبكة لموقع خارجي (أبطأ وأقل ثباتًا في مكالمة حية).

التحديث نفسه بيحصل مرة كل يوم الساعة 8 صباحًا بتوقيت القاهرة (شوف
_daily_refresh_loop في agent.py) — الملف ده بس بيعرف "يجيب" و"يخزّن" و"يفرمت".
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("sdr-agent.pricing")

BASE_DIR = os.path.dirname(__file__)
CACHE_DIR = os.path.join(BASE_DIR, "data")

# الدول المدعومة حاليًا. لإضافة دولة جديدة: زوّد سطر هنا بنفس country_id اللي
# بيستخدمه رابط daftra.com/plans?country_id=xx، وهيتحدّث تلقائيًا مع باقي الدول.
KNOWN_COUNTRIES = ["sa", "eg"]
DEFAULT_COUNTRY = "eg"

# ترتيب الباقات زي ما ظاهر في الموقع دايمًا: أساسية -> متقدمة -> شاملة
PLAN_KEYS = ["basic", "advanced", "comprehensive"]


def _cache_path(country_id: str) -> str:
    return os.path.join(CACHE_DIR, f"daftra_pricing_{country_id}.json")


def fetch_plans(country_id: str) -> Optional[dict]:
    """يجيب ويحلل صفحة الأسعار لدولة معينة. بيرجع None لو فشل (شبكة أو تغيّر بنية الصفحة)."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("beautifulsoup4 غير مثبت — تخطي جلب الأسعار")
        return None

    url = f"https://www.daftra.com/plans?country_id={country_id}"
    try:
        resp = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("فشل تحميل صفحة الأسعار (%s): %s", country_id, e)
        return None

    try:
        soup = BeautifulSoup(resp.text, "lxml")

        titles = [t.get_text(strip=True) for t in soup.select(".web-plans-box-title")]
        # الباقة الأخيرة بتيجي معاها "الأكثر مبيعًا" ملزوقة في نفس النص
        titles = [t.replace("الأكثر مبيعًا", "").strip() for t in titles]

        price_els = soup.select(".price.plan_box")
        monthly_by_id: dict[str, str] = {}
        yearly_by_id: dict[str, str] = {}
        currency = ""
        for el in price_els:
            pid = el.get("id") or ""
            classes = el.get("class") or []
            number = el.select_one(".number")
            fraction = el.select_one(".number.fraction")
            if not number:
                continue
            amount = number.get_text(strip=True)
            if fraction:
                amount += "." + fraction.get_text(strip=True).lstrip(".")
            month_span = el.select_one(".month")
            if month_span and not currency:
                # أول سطر جوه ".month" هو رمز العملة (ج.م / ر.س)
                first_line = month_span.get_text("\n", strip=True).split("\n")[0]
                currency = first_line.strip()
            if "yearly" in classes:
                yearly_by_id[pid] = amount
            else:
                monthly_by_id[pid] = amount

        ids_in_order = list(dict.fromkeys(monthly_by_id.keys()))

        def _row_values(css_class: str) -> list[str]:
            for w in soup.select(".plans-table-title-wrap"):
                tr = w.find_parent("tr")
                if tr and css_class in (tr.get("class") or []):
                    tds = [
                        td for td in tr.find_all("td")
                        if not (td.get("class") and "has-tooltip" in td.get("class"))
                    ]
                    return [td.get_text(" ", strip=True) for td in tds]
            return []

        invoices_row = _row_values("lmt_invoices_count")
        clients_row = _row_values("lmt_clients_count")

        plans = []
        for i, key in enumerate(PLAN_KEYS):
            if i >= len(titles) or i >= len(ids_in_order):
                break
            pid = ids_in_order[i]
            plans.append({
                "key": key,
                "name": titles[i],
                "price_monthly": monthly_by_id.get(pid, ""),
                "price_monthly_when_billed_yearly": yearly_by_id.get(pid, ""),
                "invoices_and_quotes_limit": invoices_row[i] if i < len(invoices_row) else "",
                "clients_limit": clients_row[i] if i < len(clients_row) else "",
            })

        if not plans:
            logger.warning("لم يتم العثور على أي باقة أثناء تحليل صفحة الأسعار (%s) — ممكن الموقع يكون غيّر شكله", country_id)
            return None

        return {
            "country_id": country_id,
            "currency": currency,
            "source_url": url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "plans": plans,
        }
    except Exception as e:
        logger.warning("فشل تحليل صفحة الأسعار (%s): %s", country_id, e, exc_info=True)
        return None


def refresh_plans_cache(country_id: str) -> bool:
    """يجيب الأسعار الحالية ويحفظها في الملف المحلي. لو فشل، بيسيب آخر نسخة محفوظة
    كما هي (عشان مكالمة حية ما تتأثرش بمشكلة مؤقتة في الموقع)."""
    data = fetch_plans(country_id)
    if not data:
        return False
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(country_id), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("تم تحديث أسعار دفترة لدولة %s (%d باقات)", country_id, len(data["plans"]))
        return True
    except Exception as e:
        logger.warning("فشل حفظ cache الأسعار (%s): %s", country_id, e)
        return False


def get_cached_plans(country_id: str) -> Optional[dict]:
    try:
        with open(_cache_path(country_id), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def format_plans_for_prompt(country_id: str) -> str:
    """يرجّع فقرة نصية جاهزة للحقن في البرومبت (شخصية العميل أو تقييم المندوب).
    لو مفيش cache خالص (أول تشغيل قبل أول تحديث) بيرجع نص فاضي — الاستدعاء
    اللي بيستخدمها لازم يتعامل مع الحالة دي (يتجاهل القسم بدل ما يكسر البرومبت)."""
    country_id = country_id if country_id in KNOWN_COUNTRIES else DEFAULT_COUNTRY
    data = get_cached_plans(country_id)
    if not data or not data.get("plans"):
        return ""

    currency = data.get("currency", "")
    lines = [f"باقات دفترة الحالية ({currency}) — محدثة بتاريخ {data.get('fetched_at', '')[:10]}:"]
    for p in data["plans"]:
        monthly = p.get("price_monthly", "")
        yearly_m = p.get("price_monthly_when_billed_yearly", "")
        invoices = p.get("invoices_and_quotes_limit", "")
        clients = p.get("clients_limit", "")
        lines.append(
            f"- {p['name']}: {monthly} {currency} شهريًا عند الدفع الشهري، "
            f"أو {yearly_m} {currency}/شهر عند الدفع سنويًا مقدمًا. "
            f"الفواتير وعروض الأسعار: {invoices}. عدد العملاء: {clients}."
        )
    return "\n".join(lines)
