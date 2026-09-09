#!/bin/sh
# ============================================================
#  HiringFlow AI Simulator — Production Entrypoint
#  Starts both FastAPI server and LiveKit agent in one container.
# ============================================================
set -e

echo "[entrypoint] Starting HiringFlow AI Simulator..."

# Start the FastAPI server in the background
python server.py &
SERVER_PID=$!

# Wait for server to be ready
echo "[entrypoint] Waiting for server to start..."
sleep 3

# Start the LiveKit agent in PRODUCTION mode (not dev)
# "start" enables: load limiting, pre-warmed processes, graceful drain, JSON logs
python agent.py start &
AGENT_PID=$!

echo "[entrypoint] Server PID=$SERVER_PID, Agent PID=$AGENT_PID"

# Handle shutdown signals
cleanup() {
    echo "[entrypoint] Shutting down..."
    kill $AGENT_PID 2>/dev/null || true
    kill $SERVER_PID 2>/dev/null || true
    wait $AGENT_PID 2>/dev/null || true
    wait $SERVER_PID 2>/dev/null || true
    echo "[entrypoint] Shutdown complete."
}
trap cleanup SIGTERM SIGINT

# Monitor both processes — exit if either dies
while true; do
    if ! kill -0 $SERVER_PID 2>/dev/null; then
        echo "[entrypoint] Server process died"
        cleanup
        exit 1
    fi
    if ! kill -0 $AGENT_PID 2>/dev/null; then
        echo "[entrypoint] Agent process died"
        cleanup
        exit 1
    fi
    sleep 2
done
