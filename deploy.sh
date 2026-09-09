#!/usr/bin/env bash
# ============================================================
#  HiringFlow AI Simulator — Deployment Script
#
#  First time:
#    cp .env.example .env
#    nano .env              # Add your real API keys
#    ./deploy.sh
#
#  Updates:
#    git pull && ./deploy.sh
# ============================================================
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "Error: .env file not found."
  echo "Create it from the example:  cp .env.example .env && nano .env"
  exit 1
fi

echo "Building and starting HiringFlow AI Simulator..."
docker compose up -d --build

echo ""
echo "Services:"
docker compose ps

echo ""
echo "Logs (Ctrl+C to stop watching):"
docker compose logs -f --tail=20
