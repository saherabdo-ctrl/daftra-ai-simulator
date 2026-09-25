#!/bin/sh
# ============================================================
#  Prepares the state volume, then runs the service's command
#  (web: python server.py · agent: python agent.py start).
#  See the Dockerfile for why data/ paths are symlinks into /app/var.
# ============================================================
set -e

STATE=/app/var
mkdir -p "$STATE/results" "$STATE/scenarios" "$STATE/calls" "${RECORDINGS_PATH:-/recordings}"

# The dialect corpus is learned from calls, so it must survive redeploys;
# a brand-new volume starts from the copy shipped in the repo.
if [ ! -f "$STATE/dialect_corpus.json" ]; then
    cp /app/seed/dialect_corpus.json "$STATE/dialect_corpus.json"
    echo "[entrypoint] seeded dialect_corpus.json into the state volume"
fi

exec "$@"
