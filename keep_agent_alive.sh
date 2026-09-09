#!/usr/bin/env bash
# يعيد تشغيل الوكيل الآلي إذا توقف — يُنفَّذ كل 5 دقائق عبر cron وبعد إقلاع الخادم.
cd "$(dirname "$0")"

if ! pgrep -f "agent.py dev" >/dev/null 2>&1; then
  nohup python agent.py dev >> agent.log 2>&1 &
  echo "$(date): agent restarted" >> keep_agent_alive.log
fi