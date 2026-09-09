#!/usr/bin/env bash
# إعداد المشروع على خادم Serv00 — يُنفَّذ مرة واحدة بعد رفع الملفات عبر SSH.
set -e
cd "$(dirname "$0")"

echo "==> 1) تفعيل تشغيل برامجنا (Binexec)"
devil binexec on || true
echo "    ملاحظة: سجّل الخروج ثم الدخول مجددًا بعد الانتهاء ليعمل التنفيذ."

echo "==> 2) ترتيب الملفات الثابتة في public/ (Serv00 يخدمها مباشرة)"
mkdir -p public
if [ -d static ] && [ ! -f public/index.html ]; then
  cp -r static/. public/
  echo "    نُقلت الملفات الثابتة إلى public/"
fi

echo "==> 3) إنشاء البيئة الافتراضية في ~/.virtualenvs/sdr"
mkdir -p ~/.virtualenvs
if [ ! -f ~/.virtualenvs/sdr/bin/python ]; then
  if command -v virtualenv >/dev/null 2>&1; then
    virtualenv ~/.virtualenvs/sdr -p "$(command -v python3.11 || command -v python3 || command -v python)"
  else
    python3.11 -m venv ~/.virtualenvs/sdr
  fi
fi
VENV_PY=~/.virtualenvs/sdr/bin/python
"$VENV_PY" -m pip install --upgrade pip

echo "==> 4) تثبيت المتطلبات (قد يستغرق بضع دقائق)"
"$VENV_PY" -m pip install -r requirements.txt

echo "==> 5) ملف المتغيرات .env"
if [ ! -f .env ]; then
  cp .env.serv00.example .env
  echo "    أنشئ .env من المثال — عدّله بمفاتيحك ثم أعد التشغيل."
fi

chmod +x keep_agent_alive.sh

echo ""
echo "============================================================"
echo " الإعداد اليدوي المتبقي (من لوحة DevilWEB):"
echo "============================================================"
echo " أ) WWW → أضف موقعًا نوع Python (Phusion Passenger):"
echo "    - النطاق: SUBDOMAIN.serv00.net"
echo "    - مجلد الموقع: public_python (هذا المجلد)"
echo "    - المفسّر: $VENV_PY"
echo "    - عدد العمليات: 1"
echo "    ثم Restart من اللوحة."
echo ""
echo " ب) أعد الدخول SSH، ثم شغّل الوكيل واجعله دائمًا:"
echo "    nohup ./keep_agent_alive.sh &"
echo ""
echo " ج) أضف cron ليبقى كل شيء يعمل (crontab -e):"
echo "    */5 * * * *  /home/LOGIN/domains/SUBDOMAIN.serv00.net/public_python/keep_agent_alive.sh"
echo "    */15 * * * * curl -s -o /dev/null https://SUBDOMAIN.serv00.net/"
echo "    @reboot sleep 30 && /home/LOGIN/domains/SUBDOMAIN.serv00.net/public_python/keep_agent_alive.sh"
echo "============================================================"