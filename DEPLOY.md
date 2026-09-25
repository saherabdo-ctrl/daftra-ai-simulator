# Deploying Daftra AI Simulator

Production: **https://enezam.com** — deployed from `main`.

## How it works

| Piece | What it does |
|---|---|
| `Dockerfile` | One image (Python 3.12, ffmpeg, non-root). |
| `docker-compose.yml` | Two services from that image: **web** (`python server.py`, FastAPI on :8000, the only public service) and **agent** (`python agent.py start`, the LiveKit voice agent — outbound to LiveKit Cloud, no port). Each restarts independently. |
| `docker/entrypoint.sh` | Prepares the state volume, then runs the service. |
| `.github/workflows/deploy.yml` | Every push/PR to `main`: compile check + image build. Every push to `main`: deploy, then verify `https://enezam.com/health`. |

**Merging to `main` deploys production.** The server builds the commit,
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

## Configuration

The `.env` lives only on the server (never in git). Internal URLs
(`EVALUATION_WEBHOOK_URL`, `SCENARIOS_BASE_URL`) and `RECORDINGS_PATH` are
fixed in `docker-compose.yml`. The Google service-account key is a file on the
server, mounted read-only; `GOOGLE_CREDENTIALS` points at it.

## Local run

```bash
cp .env.example .env    # fill in keys
docker compose up -d --build
# http://localhost:8000
```

## GitHub secrets (one-time)

`DEPLOY_HOST`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` — the SSH key belongs
to a server user that can only run this project's deploy command.
