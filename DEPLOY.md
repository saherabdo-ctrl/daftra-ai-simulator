# Deploying Daftra AI Simulator

| Environment | URL | Branch |
|---|---|---|
| Production | https://enezam.com | `main` |
| Staging | https://staging.enezam.com | `staging` |

Test on staging first: merge into `staging`, make a real call, then merge
`staging` into `main`.

## How it works

| Piece | What it does |
|---|---|
| `Dockerfile` | One image (Python 3.12, ffmpeg, non-root). |
| `docker-compose.yml` | Two services from that image: **web** (`python server.py`, FastAPI on :8000, the only public service) and **agent** (`python agent.py start`, the LiveKit voice agent — outbound to LiveKit Cloud, no port). Each restarts independently. |
| `docker/entrypoint.sh` | Prepares the state volume, then runs the service. |
| `.github/workflows/deploy.yml` | Every push/PR to `main` or `staging`: compile check + image build. Every push to `main` / `staging`: deploy to that environment, then verify its `/health`. |

**Merging to `main` deploys production** (and to `staging`, staging). The server builds the commit,
starts it, health-checks it and **rolls back automatically** if it fails — a
red workflow means the site stayed on the last good commit, not that it is
down. A poller on the server also picks up any push the workflow missed.

## Data

Runtime state lives in Docker volumes, not in the image:

- `state` → `/app/var`: `results/`, `scenarios/`, `calls/`, `users.json`, and
  `dialect_corpus.json` (learned from calls; a new volume starts from the
  repo copy). The code still uses its `data/…` paths — they are symlinks.
- `recordings` → `/recordings`: call recordings (agent writes, web serves).

`data/daftra_knowledge.md` is **not** in a volume: it ships with the image,
so editing it in git and merging updates production.

## Calls never stay "in progress"

A call is "in progress" until the agent posts its evaluation. Three layers
make sure that always happens:

1. **Finalization survives shutdown.** Recording upload, evaluation and the
   Calls-sheet update run as one task that LiveKit's `on_session_end` hook
   waits for (up to 300s), even when the call was cut off, the agent was
   force-disconnected or the worker is stopping. If there is still no result
   at the end, the call is marked **failed**.
2. **Deploys don't cut calls.** On stop, the agent drains: no new calls,
   running calls finish and finalize (`stop_grace_period: 12m`). A deploy
   that happens during a call therefore waits for it.
3. **Sweeper.** The web service checks every 5 minutes and closes any call
   still "in progress" past its classification's max duration + 15 minutes
   as **interrupted** (`STALE_CALL_*` in `.env.example`).

If every LLM fails for a turn, the AI client says a short "sorry, can you
repeat that?" instead of going silent.

## Alerts

Set `ALERT_WEBHOOK_URL` (Slack or Google Chat incoming webhook) and
`ALERT_ENV_LABEL` in the server `.env` to get a message when: the agent is
force-stopped mid-call, all LLMs fail, an AI provider rejects its API key, a
call ends without a result, or the sweeper closes stuck calls. Each alert
repeats at most every 15 minutes.

## Configuration

The `.env` lives only on the server (never in git). Internal URLs
(`EVALUATION_WEBHOOK_URL`, `SCENARIOS_BASE_URL`) and `RECORDINGS_PATH` are
fixed in `docker-compose.yml`. The Google service-account key is a file on the
server, mounted read-only; `GOOGLE_CREDENTIALS` points at it.

Staging has its **own** `.env`, Google Sheet and `AGENT_NAME` — it must never
write into the production sheet or answer production calls.

## Local run

```bash
cp .env.example .env    # fill in keys
docker compose up -d --build
# http://localhost:8000
```

## GitHub secrets (one-time)

`DEPLOY_HOST`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` — the SSH key belongs
to a server user that can only run this project's deploy command.
